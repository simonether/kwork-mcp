from __future__ import annotations

import json
import os
import sqlite3
import stat
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from kwork_mcp.config import KworkConfig
from kwork_mcp.coordination import CoordinationStore, CursorCodec, pages_from_paging
from kwork_mcp.errors import GatewayError
from kwork_mcp.models import ErrorCode, ErrorInfo, WriteAction, WriteState


def _create_legacy_database(path: Path, version: int) -> None:
    path.touch(mode=0o600)
    os.chmod(path, 0o600)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            """
            CREATE TABLE circuits (
                scope TEXT NOT NULL,
                route TEXT NOT NULL,
                failures INTEGER NOT NULL,
                open_until REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (scope, route)
            )
            """
        )
        connection.execute(f"PRAGMA user_version={version}")
        connection.commit()


def test_coordination_migrates_schema_v1_and_rejects_unknown_schema(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory()
    database = config.state_dir / "coordination.sqlite3"
    _create_legacy_database(database, 1)
    store = CoordinationStore(config)
    with closing(store._connect()) as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(circuits)").fetchall()}
        assert {"probe_owner", "probe_until"} <= columns
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3

    other = config_factory()
    unknown_database = other.state_dir / "coordination.sqlite3"
    _create_legacy_database(unknown_database, 99)
    with pytest.raises(GatewayError) as caught:
        CoordinationStore(other)
    assert caught.value.code is ErrorCode.CONTRACT_DRIFT
    assert caught.value.diagnostic == "coordination_schema=99"


@pytest.mark.asyncio
async def test_v2_committing_write_migrates_to_unknown_and_blocks_duplicates(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory()
    initial = CoordinationStore(config)
    now = time.time()
    with closing(initial._connect()) as connection:
        connection.execute("ALTER TABLE writes DROP COLUMN remote_started_at")
        connection.execute(
            """
            INSERT INTO writes(
                write_id,scope,idempotency_key,action,payload_json,payload_hash,
                confirmation_hash,state,prepared_at,expires_at,updated_at,
                lease_owner,lease_expires,result_json,error_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "00000000-0000-4000-8000-000000000001",
                "account-42",
                "legacy-committing",
                WriteAction.MARK_DIALOG_READ.value,
                json.dumps(
                    {
                        "request": {
                            "action": WriteAction.MARK_DIALOG_READ.value,
                            "user_id": 7,
                        },
                        "resolved": {},
                        "prepared_account_id": 42,
                    },
                    sort_keys=True,
                ),
                "a" * 64,
                "b" * 64,
                WriteState.COMMITTING.value,
                now,
                now + 600,
                now,
                "legacy-owner",
                now + 120,
                None,
                None,
            ),
        )
        connection.execute(
            """
            INSERT INTO write_events(write_id,state,occurred_at,note)
            VALUES(?,?,?,?)
            """,
            (
                "00000000-0000-4000-8000-000000000001",
                WriteState.COMMITTING.value,
                now,
                "legacy-claimed",
            ),
        )
        prepared_payload = {
            "request": {
                "action": WriteAction.DELETE_OFFER.value,
                "offer_id": 99,
            },
            "resolved": {},
            "prepared_account_id": 99,
        }
        prepared_json, prepared_hash = initial.canonical_payload(prepared_payload)
        connection.execute(
            """
            INSERT INTO writes(
                write_id,scope,idempotency_key,action,payload_json,payload_hash,
                confirmation_hash,state,prepared_at,expires_at,updated_at,
                lease_owner,lease_expires,result_json,error_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "00000000-0000-4000-8000-000000000002",
                "account-99",
                "legacy-prepared",
                WriteAction.DELETE_OFFER.value,
                prepared_json,
                prepared_hash,
                "legacy-random-confirmation-hash",
                WriteState.PREPARED.value,
                now,
                now + 600,
                now,
                None,
                None,
                None,
                None,
            ),
        )
        connection.execute(
            """
            INSERT INTO write_events(write_id,state,occurred_at,note)
            VALUES(?,?,?,?)
            """,
            (
                "00000000-0000-4000-8000-000000000002",
                WriteState.PREPARED.value,
                now,
                "legacy-prepared",
            ),
        )
        connection.execute("PRAGMA user_version=2")
        connection.commit()

    migrated = CoordinationStore(config)
    record = await migrated.get_write(
        "00000000-0000-4000-8000-000000000001",
        scope="account-42",
    )
    assert record is not None
    assert record.state is WriteState.SUBMISSION_UNKNOWN
    assert record.remote_started_at == now
    assert record.lease_owner is None
    assert record.lease_expires is None
    assert record.error_json is not None

    second = await migrated.prepare_write(
        scope="account-42",
        idempotency_key="after-legacy-unknown",
        action=WriteAction.MARK_DIALOG_READ,
        payload={
            "request": {
                "action": WriteAction.MARK_DIALOG_READ.value,
                "user_id": 8,
            },
            "resolved": {},
            "prepared_account_id": 42,
        },
    )
    assert second.confirmation_token is not None
    with pytest.raises(GatewayError) as blocked:
        await migrated.claim_write(
            write_id=second.record.write_id,
            scope="account-42",
            payload_hash=second.record.payload_hash,
            confirmation_token=second.confirmation_token,
            owner="new-owner",
        )
    assert blocked.value.code is ErrorCode.AMBIGUOUS_WRITE
    assert blocked.value.reconciliation_required is True

    replay = await migrated.prepare_write(
        scope="account-99",
        idempotency_key="legacy-prepared",
        action=WriteAction.DELETE_OFFER,
        payload=prepared_payload,
    )
    assert replay.record.write_id == "00000000-0000-4000-8000-000000000002"
    assert replay.confirmation_token is not None
    claimed = await migrated.claim_write(
        write_id=replay.record.write_id,
        scope="account-99",
        payload_hash=replay.record.payload_hash,
        confirmation_token=replay.confirmation_token,
        owner="migrated-owner",
    )
    assert claimed.state is WriteState.COMMITTING


def test_coordination_rejects_symlink_permissions_and_post_init_tamper(
    config_factory: Callable[..., KworkConfig],
    tmp_path: Path,
) -> None:
    config = config_factory()
    database = config.state_dir / "coordination.sqlite3"
    target = tmp_path / "database"
    target.touch(mode=0o600)
    database.symlink_to(target)
    with pytest.raises(GatewayError) as symlink:
        CoordinationStore(config)
    assert symlink.value.diagnostic == "coordination_db_not_regular"

    config = config_factory()
    database = config.state_dir / "coordination.sqlite3"
    database.touch(mode=0o644)
    os.chmod(database, 0o644)
    with pytest.raises(GatewayError) as permissions:
        CoordinationStore(config)
    assert permissions.value.diagnostic == "coordination_db_permissions"
    assert stat.S_IMODE(database.stat().st_mode) == 0o644

    store = CoordinationStore(config_factory())
    os.chmod(store.path, 0o644)
    with pytest.raises(GatewayError) as changed:
        store._connect()
    assert changed.value.diagnostic == "coordination_db_security_changed"


def test_fresh_coordination_database_is_private_under_restrictive_umask(
    tmp_path: Path,
) -> None:
    config = KworkConfig(
        token="fixture",
        persist_token=False,
        state_dir=tmp_path / "coordination-umask-state",
    )

    old_umask = os.umask(0o777)
    try:
        store = CoordinationStore(config)
    finally:
        os.umask(old_umask)

    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    with closing(store._connect()) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3


def test_transaction_rolls_back_on_failure(
    config_factory: Callable[..., KworkConfig],
) -> None:
    store = CoordinationStore(config_factory())

    def operation(connection: sqlite3.Connection) -> None:
        connection.execute("INSERT INTO metadata(key,value) VALUES('rolled-back','value')")
        raise RuntimeError("stop")

    with pytest.raises(RuntimeError, match="stop"):
        store._transaction(operation)
    with closing(store._connect()) as connection:
        assert connection.execute("SELECT value FROM metadata WHERE key='rolled-back'").fetchone() is None


@pytest.mark.asyncio
async def test_secret_is_stable_and_rate_limit_timeout_is_safe(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory(
        rps_limit=0.01,
        burst_limit=1,
        route_rps_limit=0.01,
        route_burst_limit=1,
        rate_wait_timeout=0.1,
    )
    first = CoordinationStore(config)
    secret = await first.get_or_create_secret("fixture", byte_length=18)
    assert len(secret) == 18
    assert await CoordinationStore(config).get_or_create_secret("fixture") == secret

    await first.acquire("account-42", "projects")
    with pytest.raises(GatewayError) as caught:
        await first.acquire("account-42", "projects")
    assert caught.value.code is ErrorCode.RATE_LIMIT
    assert caught.value.safe_to_retry is True
    assert caught.value.retry_after_seconds is not None


@pytest.mark.asyncio
async def test_circuit_retry_after_and_exponential_cap(
    config_factory: Callable[..., KworkConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_factory(
        circuit_failure_threshold=1,
        circuit_open_seconds=900,
    )
    store = CoordinationStore(config)
    now = 1_000.0
    monkeypatch.setattr("kwork_mcp.coordination.time.time", lambda: now)
    await store.record_failure("scope", "route", retry_after_seconds=950.0)
    with pytest.raises(GatewayError) as caught:
        await store.check_circuit("scope", "route")
    assert caught.value.retry_after_seconds == pytest.approx(900.0)

    await store.record_failure("scope", "other")
    await store.record_failure("scope", "other")
    with closing(store._connect()) as connection:
        row = connection.execute("SELECT open_until FROM circuits WHERE scope='scope' AND route='other'").fetchone()
    assert float(row["open_until"]) - now == 900.0


@pytest.mark.asyncio
async def test_writer_guard_releases_after_exception(
    config_factory: Callable[..., KworkConfig],
) -> None:
    store = CoordinationStore(config_factory())
    with pytest.raises(RuntimeError, match="boom"):
        async with store.writer_guard("account-42"):
            raise RuntimeError("boom")
    async with store.writer_guard("account-42"):
        pass


@pytest.mark.asyncio
async def test_prepare_expiry_retention_and_idempotency_lookup(
    config_factory: Callable[..., KworkConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_factory(preparation_ttl_seconds=30, state_retention_days=1)
    store = CoordinationStore(config)
    start = time.time()
    monkeypatch.setattr("kwork_mcp.coordination.time.time", lambda: start)
    prepared = await store.prepare_write(
        scope="account-42",
        idempotency_key="expiry-key",
        action=WriteAction.DELETE_OFFER,
        payload={"request": {"action": "delete_offer", "offer_id": 1}},
    )
    assert (
        await store.get_write_by_idempotency(
            scope="account-42",
            idempotency_key="expiry-key",
        )
        == prepared.record
    )
    assert (
        await store.get_write_by_idempotency(
            scope="account-99",
            idempotency_key="expiry-key",
        )
        is None
    )

    monkeypatch.setattr("kwork_mcp.coordination.time.time", lambda: start + 31)
    replay = await store.prepare_write(
        scope="account-42",
        idempotency_key="expiry-key",
        action=WriteAction.DELETE_OFFER,
        payload={"request": {"action": "delete_offer", "offer_id": 1}},
    )
    assert replay.record.state is WriteState.EXPIRED
    assert replay.confirmation_token is None

    monkeypatch.setattr(
        "kwork_mcp.coordination.time.time",
        lambda: start + 31 + 2 * 86_400,
    )
    await store.prepare_write(
        scope="account-42",
        idempotency_key="new-key",
        action=WriteAction.DELETE_OFFER,
        payload={"request": {"action": "delete_offer", "offer_id": 2}},
    )
    assert await store.get_write(prepared.record.write_id, scope="account-42") is None
    assert await store.count_events(prepared.record.write_id) == 0


@pytest.mark.asyncio
async def test_get_write_persists_preparation_ttl_expiry(
    config_factory: Callable[..., KworkConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CoordinationStore(config_factory(preparation_ttl_seconds=30))
    now = time.time()
    monkeypatch.setattr("kwork_mcp.coordination.time.time", lambda: now)
    prepared = await store.prepare_write(
        scope="account-42",
        idempotency_key="status-expiry",
        action=WriteAction.DELETE_OFFER,
        payload={"request": {"action": "delete_offer", "offer_id": 1}},
    )
    monkeypatch.setattr("kwork_mcp.coordination.time.time", lambda: now + 31)
    expired = await store.get_write(prepared.record.write_id, scope="account-42")
    assert expired is not None and expired.state is WriteState.EXPIRED
    assert await store.count_events(prepared.record.write_id) == 2


@pytest.mark.asyncio
async def test_recover_stale_write_requires_expired_lease_and_free_writer_guard(
    config_factory: Callable[..., KworkConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CoordinationStore(config_factory(write_lease_seconds=10))
    now = time.time()
    monkeypatch.setattr("kwork_mcp.coordination.time.time", lambda: now)
    write_id, _payload_hash, _confirmation = await _prepared_and_claimed(store)
    assert (
        await store.recover_stale_write(
            write_id="missing",
            scope="account-42",
        )
        is None
    )
    live = await store.recover_stale_write(
        write_id=write_id,
        scope="account-42",
    )
    assert live is not None and live.state is WriteState.COMMITTING
    await store.mark_write_remote_started(
        write_id=write_id,
        scope="account-42",
        owner="owner",
    )

    monkeypatch.setattr("kwork_mcp.coordination.time.time", lambda: now + 11)
    async with store.writer_guard("account-42"):
        recovered = await store.recover_stale_write(
            write_id=write_id,
            scope="account-42",
        )
    assert recovered is not None
    assert recovered.state is WriteState.SUBMISSION_UNKNOWN
    assert recovered.lease_owner is None
    assert json.loads(recovered.error_json or "{}")["code"] == "ambiguous_write"


async def _prepared_and_claimed(
    store: CoordinationStore,
    *,
    key: str = "claimed-key",
    scope: str = "account-42",
    owner: str = "owner",
) -> tuple[str, str, str]:
    prepared = await store.prepare_write(
        scope=scope,
        idempotency_key=key,
        action=WriteAction.DELETE_OFFER,
        payload={"request": {"action": "delete_offer", "offer_id": 1}},
    )
    assert prepared.confirmation_token
    await store.claim_write(
        write_id=prepared.record.write_id,
        scope=scope,
        payload_hash=prepared.record.payload_hash,
        confirmation_token=prepared.confirmation_token,
        owner=owner,
    )
    return (
        prepared.record.write_id,
        prepared.record.payload_hash,
        prepared.confirmation_token,
    )


@pytest.mark.asyncio
async def test_claim_rejects_bad_confirmation_and_marks_same_stale_write_unknown(
    config_factory: Callable[..., KworkConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_factory(write_lease_seconds=10)
    store = CoordinationStore(config)
    now = time.time()
    monkeypatch.setattr("kwork_mcp.coordination.time.time", lambda: now)
    prepared = await store.prepare_write(
        scope="account-42",
        idempotency_key="confirmation-key",
        action=WriteAction.DELETE_OFFER,
        payload={"request": {"action": "delete_offer", "offer_id": 1}},
    )
    assert prepared.confirmation_token
    with pytest.raises(GatewayError) as bad_hash:
        await store.claim_write(
            write_id=prepared.record.write_id,
            scope="account-42",
            payload_hash="0" * 64,
            confirmation_token=prepared.confirmation_token,
            owner="owner",
        )
    assert bad_hash.value.code is ErrorCode.INVALID_CONFIRMATION
    with pytest.raises(GatewayError) as bad_token:
        await store.claim_write(
            write_id=prepared.record.write_id,
            scope="account-42",
            payload_hash=prepared.record.payload_hash,
            confirmation_token="wrong",
            owner="owner",
        )
    assert bad_token.value.code is ErrorCode.INVALID_CONFIRMATION

    await store.claim_write(
        write_id=prepared.record.write_id,
        scope="account-42",
        payload_hash=prepared.record.payload_hash,
        confirmation_token=prepared.confirmation_token,
        owner="owner",
    )
    await store.mark_write_remote_started(
        write_id=prepared.record.write_id,
        scope="account-42",
        owner="owner",
    )
    monkeypatch.setattr("kwork_mcp.coordination.time.time", lambda: now + 11)
    stale = await store.claim_write(
        write_id=prepared.record.write_id,
        scope="account-42",
        payload_hash=prepared.record.payload_hash,
        confirmation_token=prepared.confirmation_token,
        owner="owner",
    )
    assert stale.state is WriteState.SUBMISSION_UNKNOWN
    assert json.loads(stale.error_json or "{}")["code"] == ErrorCode.AMBIGUOUS_WRITE


@pytest.mark.asyncio
async def test_claim_expired_preparation_and_active_other_writer(
    config_factory: Callable[..., KworkConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_factory(preparation_ttl_seconds=30, write_lease_seconds=10)
    store = CoordinationStore(config)
    now = time.time()
    monkeypatch.setattr("kwork_mcp.coordination.time.time", lambda: now)
    first = await store.prepare_write(
        scope="account-42",
        idempotency_key="active-first",
        action=WriteAction.DELETE_OFFER,
        payload={"request": {"action": "delete_offer", "offer_id": 1}},
    )
    second = await store.prepare_write(
        scope="account-42",
        idempotency_key="active-second",
        action=WriteAction.DELETE_OFFER,
        payload={"request": {"action": "delete_offer", "offer_id": 2}},
    )
    assert first.confirmation_token and second.confirmation_token
    await store.claim_write(
        write_id=first.record.write_id,
        scope="account-42",
        payload_hash=first.record.payload_hash,
        confirmation_token=first.confirmation_token,
        owner="first",
    )
    with pytest.raises(GatewayError) as active:
        await store.claim_write(
            write_id=second.record.write_id,
            scope="account-42",
            payload_hash=second.record.payload_hash,
            confirmation_token=second.confirmation_token,
            owner="second",
        )
    assert active.value.code is ErrorCode.WRITE_IN_PROGRESS
    assert active.value.retry_after_seconds == pytest.approx(10.0)

    other = await store.prepare_write(
        scope="account-99",
        idempotency_key="expires-before-claim",
        action=WriteAction.DELETE_OFFER,
        payload={"request": {"action": "delete_offer", "offer_id": 3}},
    )
    assert other.confirmation_token
    monkeypatch.setattr("kwork_mcp.coordination.time.time", lambda: now + 31)
    expired = await store.claim_write(
        write_id=other.record.write_id,
        scope="account-99",
        payload_hash=other.record.payload_hash,
        confirmation_token=other.confirmation_token,
        owner="owner",
    )
    assert expired.state is WriteState.EXPIRED


@pytest.mark.asyncio
async def test_finish_write_validates_state_owner_and_serializes_error(
    config_factory: Callable[..., KworkConfig],
) -> None:
    store = CoordinationStore(config_factory())
    with pytest.raises(ValueError, match="invalid finish state"):
        await store.finish_write(
            write_id="missing",
            scope="account-42",
            owner="owner",
            state=WriteState.PREPARED,
            note="invalid",
        )
    with pytest.raises(GatewayError) as missing:
        await store.finish_write(
            write_id="missing",
            scope="account-42",
            owner="owner",
            state=WriteState.FAILED_KNOWN,
            note="missing",
        )
    assert missing.value.code is ErrorCode.NOT_FOUND

    write_id, _payload_hash, _confirmation = await _prepared_and_claimed(store)
    with pytest.raises(GatewayError) as lost:
        await store.finish_write(
            write_id=write_id,
            scope="account-42",
            owner="different",
            state=WriteState.FAILED_KNOWN,
            note="lost",
        )
    assert lost.value.code is ErrorCode.AMBIGUOUS_WRITE

    error = ErrorInfo(
        code=ErrorCode.PERMISSION,
        message="safe",
        correlation_id="correlation",
    )
    failed = await store.finish_write(
        write_id=write_id,
        scope="account-42",
        owner="owner",
        state=WriteState.FAILED_KNOWN,
        error=error,
        note="known",
    )
    assert json.loads(failed.error_json or "{}")["code"] == "permission"


@pytest.mark.asyncio
async def test_release_claim_returns_prepared_or_expired_and_checks_owner(
    config_factory: Callable[..., KworkConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_factory(preparation_ttl_seconds=30)
    store = CoordinationStore(config)
    now = time.time()
    monkeypatch.setattr("kwork_mcp.coordination.time.time", lambda: now)
    write_id, _payload_hash, _confirmation = await _prepared_and_claimed(store)
    with pytest.raises(GatewayError) as lost:
        await store.release_write_claim(
            write_id=write_id,
            scope="account-42",
            owner="different",
            note="lost",
        )
    assert lost.value.code is ErrorCode.AMBIGUOUS_WRITE
    released = await store.release_write_claim(
        write_id=write_id,
        scope="account-42",
        owner="owner",
        note="retry",
    )
    assert released.state is WriteState.PREPARED

    second_id, _payload_hash, _confirmation = await _prepared_and_claimed(
        store,
        key="expires-on-release",
        scope="account-99",
    )
    monkeypatch.setattr("kwork_mcp.coordination.time.time", lambda: now + 31)
    expired = await store.release_write_claim(
        write_id=second_id,
        scope="account-99",
        owner="owner",
        note="expired",
    )
    assert expired.state is WriteState.EXPIRED
    with pytest.raises(GatewayError) as missing:
        await store.release_write_claim(
            write_id="missing",
            scope="account-42",
            owner="owner",
            note="missing",
        )
    assert missing.value.code is ErrorCode.NOT_FOUND


@pytest.mark.asyncio
async def test_reconcile_visibility_interval_success_and_idempotent_terminal(
    config_factory: Callable[..., KworkConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_factory(
        reconciliation_min_age_seconds=5,
        reconciliation_absence_interval_seconds=5,
    )
    store = CoordinationStore(config)
    now = time.time()
    monkeypatch.setattr("kwork_mcp.coordination.time.time", lambda: now)
    write_id, _payload_hash, _confirmation = await _prepared_and_claimed(store)
    await store.finish_write(
        write_id=write_id,
        scope="account-42",
        owner="owner",
        state=WriteState.SUBMISSION_UNKNOWN,
        note="timeout",
    )
    with pytest.raises(ValueError, match="invalid reconciliation state"):
        await store.reconcile_write(
            write_id=write_id,
            scope="account-42",
            state=WriteState.SUCCEEDED,
            result=None,
            note="invalid",
        )
    with pytest.raises(GatewayError) as too_young:
        await store.reconcile_write(
            write_id=write_id,
            scope="account-42",
            state=WriteState.RECONCILED_ABSENT,
            result=None,
            note="absent",
        )
    assert too_young.value.diagnostic == "reconciliation_visibility_window"

    monkeypatch.setattr("kwork_mcp.coordination.time.time", lambda: now + 5)
    first = await store.reconcile_write(
        write_id=write_id,
        scope="account-42",
        state=WriteState.RECONCILED_ABSENT,
        result={"absent": True},
        note="absent",
    )
    assert first.state is WriteState.SUBMISSION_UNKNOWN
    with pytest.raises(GatewayError) as interval:
        await store.reconcile_write(
            write_id=write_id,
            scope="account-42",
            state=WriteState.RECONCILED_ABSENT,
            result={"absent": True},
            note="absent",
        )
    assert interval.value.diagnostic == "reconciliation_absence_interval"

    monkeypatch.setattr("kwork_mcp.coordination.time.time", lambda: now + 10)
    terminal = await store.reconcile_write(
        write_id=write_id,
        scope="account-42",
        state=WriteState.RECONCILED_ABSENT,
        result={"absent": True},
        note="absent",
    )
    assert terminal.state is WriteState.RECONCILED_ABSENT
    again = await store.reconcile_write(
        write_id=write_id,
        scope="account-42",
        state=WriteState.RECONCILED_SUCCEEDED,
        result={"found": True},
        note="late",
    )
    assert again.state is WriteState.RECONCILED_ABSENT

    second_id, _hash, _token = await _prepared_and_claimed(
        store,
        key="reconcile-success",
        scope="account-99",
    )
    await store.finish_write(
        write_id=second_id,
        scope="account-99",
        owner="owner",
        state=WriteState.SUBMISSION_UNKNOWN,
        note="timeout",
    )
    monkeypatch.setattr("kwork_mcp.coordination.time.time", lambda: now + 20)
    succeeded = await store.reconcile_write(
        write_id=second_id,
        scope="account-99",
        state=WriteState.RECONCILED_SUCCEEDED,
        result={"offer_id": 123},
        note="found",
    )
    assert succeeded.state is WriteState.RECONCILED_SUCCEEDED
    assert json.loads(succeeded.result_json or "{}") == {"offer_id": 123}

    with pytest.raises(GatewayError) as missing:
        await store.reconcile_write(
            write_id="missing",
            scope="account-42",
            state=WriteState.RECONCILED_SUCCEEDED,
            result=None,
            note="missing",
        )
    assert missing.value.code is ErrorCode.NOT_FOUND


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cursor",
    [
        "not-two-parts",
        "!!!.!!!",
    ],
)
async def test_cursor_rejects_malformed_encodings(
    config_factory: Callable[..., KworkConfig],
    cursor: str,
) -> None:
    codec = CursorCodec(CoordinationStore(config_factory()))
    with pytest.raises(GatewayError) as caught:
        await codec.decode(cursor)
    assert caught.value.code is ErrorCode.VALIDATION


@pytest.mark.parametrize(
    ("paging", "item_count", "expected"),
    [
        (
            {"page": "2", "limit": 0, "total": -1, "pages": -1},
            0,
            (1, None, None, None),
        ),
        ({"page": None, "limit": "bad", "total": "bad"}, 3, (1, 3, None, None)),
        (
            {"page": True, "limit": True, "total": False, "pages": True},
            0,
            (1, None, None, None),
        ),
    ],
)
def test_pages_from_paging_invalid_optional_values(
    paging: dict[str, Any],
    item_count: int,
    expected: tuple[int, int | None, int | None, int | None],
) -> None:
    assert pages_from_paging(paging, item_count) == expected
