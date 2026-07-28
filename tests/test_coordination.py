from __future__ import annotations

import asyncio
import gc
import multiprocessing
import os
import sqlite3
import stat
import time
import warnings
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from kwork_mcp.config import KworkConfig
from kwork_mcp.coordination import CoordinationStore, CursorCodec, pages_from_paging
from kwork_mcp.errors import GatewayError
from kwork_mcp.models import ErrorCode, WriteAction, WriteState


@pytest.mark.asyncio
async def test_cursor_is_persistent_tamper_evident_and_filter_ready(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory()
    first = CursorCodec(CoordinationStore(config))
    cursor = await first.encode({"v": 1, "kind": "projects", "page": 2, "fingerprint": "abc"})
    second = CursorCodec(CoordinationStore(config))
    assert await second.decode(cursor) == {
        "v": 1,
        "kind": "projects",
        "page": 2,
        "fingerprint": "abc",
    }
    body, signature = cursor.split(".", 1)
    replacement = "A" if signature[0] != "A" else "B"
    tampered = f"{body}.{replacement}{signature[1:]}"
    with pytest.raises(GatewayError) as caught:
        await second.decode(tampered)
    assert caught.value.code is ErrorCode.VALIDATION


@pytest.mark.asyncio
async def test_prepare_claim_finish_is_idempotent_and_one_writer(
    config_factory: Callable[..., KworkConfig],
) -> None:
    store = CoordinationStore(config_factory())
    first = await store.prepare_write(
        scope="account-42",
        idempotency_key="delete-17",
        action=WriteAction.DELETE_OFFER,
        payload={"request": {"action": "delete_offer", "offer_id": 17}},
    )
    duplicate = await store.prepare_write(
        scope="account-42",
        idempotency_key="delete-17",
        action=WriteAction.DELETE_OFFER,
        payload={"request": {"action": "delete_offer", "offer_id": 17}},
    )
    assert duplicate.record.write_id == first.record.write_id
    assert duplicate.confirmation_token == first.confirmation_token
    assert first.confirmation_token is not None

    with pytest.raises(GatewayError) as mismatch:
        await store.prepare_write(
            scope="account-42",
            idempotency_key="delete-17",
            action=WriteAction.DELETE_OFFER,
            payload={"request": {"action": "delete_offer", "offer_id": 18}},
        )
    assert mismatch.value.code is ErrorCode.IDEMPOTENCY_CONFLICT

    claimed = await store.claim_write(
        scope="account-42",
        write_id=first.record.write_id,
        payload_hash=first.record.payload_hash,
        confirmation_token=first.confirmation_token,
        owner="writer-one",
    )
    assert claimed.state is WriteState.COMMITTING
    with pytest.raises(GatewayError) as busy:
        await store.claim_write(
            scope="account-42",
            write_id=first.record.write_id,
            payload_hash=first.record.payload_hash,
            confirmation_token=first.confirmation_token,
            owner="writer-two",
        )
    assert busy.value.code is ErrorCode.WRITE_IN_PROGRESS

    finished = await store.finish_write(
        scope="account-42",
        write_id=first.record.write_id,
        owner="writer-one",
        state=WriteState.SUCCEEDED,
        result={"deleted": True},
        note="test",
    )
    assert finished.state is WriteState.SUCCEEDED
    assert await store.count_events(first.record.write_id) == 3
    replay = await store.claim_write(
        scope="account-42",
        write_id=first.record.write_id,
        payload_hash=first.record.payload_hash,
        confirmation_token=first.confirmation_token,
        owner="writer-two",
    )
    assert replay.state is WriteState.SUCCEEDED


@pytest.mark.asyncio
async def test_idempotency_unique_race_uses_client_request_not_dynamic_preflight(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory()
    first_store = CoordinationStore(config)
    second_store = CoordinationStore(config)
    request = {"action": "delete_offer", "offer_id": 17}

    first, second = await asyncio.gather(
        first_store.prepare_write(
            scope="account-42",
            idempotency_key="same-client-intent",
            action=WriteAction.DELETE_OFFER,
            payload={
                "request": request,
                "resolved": {"observed": "first"},
                "prepared_account_id": 42,
            },
        ),
        second_store.prepare_write(
            scope="account-42",
            idempotency_key="same-client-intent",
            action=WriteAction.DELETE_OFFER,
            payload={
                "request": request,
                "resolved": {"observed": "second"},
                "prepared_account_id": 42,
            },
        ),
    )

    assert first.record.write_id == second.record.write_id
    assert first.record.payload_hash == second.record.payload_hash
    assert first.confirmation_token == second.confirmation_token


@pytest.mark.asyncio
async def test_circuit_opens_and_success_resets_it(
    config_factory: Callable[..., KworkConfig],
) -> None:
    store = CoordinationStore(config_factory(circuit_failure_threshold=2, circuit_open_seconds=10.0))
    await store.record_failure("account-42", "projects")
    await store.check_circuit("account-42", "projects")
    await store.record_failure("account-42", "projects")
    with pytest.raises(GatewayError) as caught:
        await store.check_circuit("account-42", "projects")
    assert caught.value.code is ErrorCode.CIRCUIT_OPEN
    assert caught.value.retry_after_seconds is not None
    await store.record_success("account-42", "projects")
    await store.check_circuit("account-42", "projects")


@pytest.mark.asyncio
async def test_half_open_circuit_allows_only_one_probe(
    config_factory: Callable[..., KworkConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_factory(
        circuit_failure_threshold=1,
        circuit_open_seconds=1.0,
        timeout=1.0,
    )
    first = CoordinationStore(config)
    second = CoordinationStore(config)
    now = time.time()
    monkeypatch.setattr("kwork_mcp.coordination.time.time", lambda: now)
    await first.record_failure("account-42", "projects")
    monkeypatch.setattr("kwork_mcp.coordination.time.time", lambda: now + 1.1)
    await first.check_circuit("account-42", "projects")
    with pytest.raises(GatewayError) as blocked:
        await second.check_circuit("account-42", "projects")
    assert blocked.value.code is ErrorCode.CIRCUIT_OPEN
    await first.record_success("account-42", "projects")
    await second.check_circuit("account-42", "projects")


@pytest.mark.asyncio
async def test_cross_account_ledger_denial_and_unknown_scope_barrier(
    config_factory: Callable[..., KworkConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_factory(write_lease_seconds=10)
    store = CoordinationStore(config)
    first = await store.prepare_write(
        scope="account-42",
        idempotency_key="first-write",
        action=WriteAction.DELETE_OFFER,
        payload={"request": {"action": "delete_offer", "offer_id": 1}},
    )
    second = await store.prepare_write(
        scope="account-42",
        idempotency_key="second-write",
        action=WriteAction.DELETE_OFFER,
        payload={"request": {"action": "delete_offer", "offer_id": 2}},
    )
    assert first.confirmation_token and second.confirmation_token
    assert await store.get_write(first.record.write_id, scope="account-99") is None
    with pytest.raises(GatewayError) as denied:
        await store.claim_write(
            write_id=first.record.write_id,
            scope="account-99",
            payload_hash=first.record.payload_hash,
            confirmation_token=first.confirmation_token,
            owner="intruder",
        )
    assert denied.value.code is ErrorCode.NOT_FOUND

    initial_time = time.time()
    monkeypatch.setattr("kwork_mcp.coordination.time.time", lambda: initial_time)
    await store.claim_write(
        write_id=first.record.write_id,
        scope="account-42",
        payload_hash=first.record.payload_hash,
        confirmation_token=first.confirmation_token,
        owner="crashed-writer",
    )
    await store.mark_write_remote_started(
        write_id=first.record.write_id,
        scope="account-42",
        owner="crashed-writer",
    )
    monkeypatch.setattr("kwork_mcp.coordination.time.time", lambda: initial_time + 11)
    with pytest.raises(GatewayError) as stale:
        await store.claim_write(
            write_id=second.record.write_id,
            scope="account-42",
            payload_hash=second.record.payload_hash,
            confirmation_token=second.confirmation_token,
            owner="next-writer",
        )
    assert stale.value.code is ErrorCode.AMBIGUOUS_WRITE
    persisted = await store.get_write(first.record.write_id, scope="account-42")
    assert persisted is not None and persisted.state is WriteState.SUBMISSION_UNKNOWN
    with pytest.raises(GatewayError) as barrier:
        await store.claim_write(
            write_id=second.record.write_id,
            scope="account-42",
            payload_hash=second.record.payload_hash,
            confirmation_token=second.confirmation_token,
            owner="next-writer",
        )
    assert barrier.value.diagnostic == "unresolved_account_write_barrier"

    other = await store.prepare_write(
        scope="account-99",
        idempotency_key="other-account-write",
        action=WriteAction.DELETE_OFFER,
        payload={"request": {"action": "delete_offer", "offer_id": 3}},
    )
    assert other.confirmation_token
    claimed_other = await store.claim_write(
        write_id=other.record.write_id,
        scope="account-99",
        payload_hash=other.record.payload_hash,
        confirmation_token=other.confirmation_token,
        owner="other-writer",
    )
    assert claimed_other.state is WriteState.COMMITTING


@pytest.mark.asyncio
async def test_absent_reconciliation_requires_two_observations(
    config_factory: Callable[..., KworkConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_factory(reconciliation_absence_interval_seconds=1.0)
    store = CoordinationStore(config)
    prepared = await store.prepare_write(
        scope="account-42",
        idempotency_key="unknown-delete",
        action=WriteAction.DELETE_OFFER,
        payload={"request": {"action": "delete_offer", "offer_id": 1}},
    )
    assert prepared.confirmation_token
    await store.claim_write(
        write_id=prepared.record.write_id,
        scope="account-42",
        payload_hash=prepared.record.payload_hash,
        confirmation_token=prepared.confirmation_token,
        owner="writer",
    )
    await store.finish_write(
        write_id=prepared.record.write_id,
        scope="account-42",
        owner="writer",
        state=WriteState.SUBMISSION_UNKNOWN,
        note="timeout",
    )
    now = time.time()
    monkeypatch.setattr("kwork_mcp.coordination.time.time", lambda: now + 1.1)
    first_observation = await store.reconcile_write(
        write_id=prepared.record.write_id,
        scope="account-42",
        state=WriteState.RECONCILED_ABSENT,
        result={"absent": True},
        note="read_back_absent",
    )
    assert first_observation.state is WriteState.SUBMISSION_UNKNOWN
    monkeypatch.setattr("kwork_mcp.coordination.time.time", lambda: now + 2.2)
    second_observation = await store.reconcile_write(
        write_id=prepared.record.write_id,
        scope="account-42",
        state=WriteState.RECONCILED_ABSENT,
        result={"absent": True},
        note="read_back_absent",
    )
    assert second_observation.state is WriteState.RECONCILED_ABSENT


def _rate_worker(
    state_dir: str,
    barrier: multiprocessing.synchronize.Barrier,
    results: multiprocessing.queues.Queue,
) -> None:
    config = KworkConfig(
        token="worker-token",
        state_dir=Path(state_dir),
        persist_token=False,
        rps_limit=2.0,
        burst_limit=1,
        route_rps_limit=100.0,
        route_burst_limit=10,
        rate_wait_timeout=3.0,
    )
    store = CoordinationStore(config)
    barrier.wait()
    asyncio.run(store.acquire("account-42", f"route-{os.getpid()}"))
    results.put(time.monotonic())


def _writer_guard_worker(
    state_dir: str,
    ready: multiprocessing.queues.Queue,
    release: multiprocessing.queues.Queue,
) -> None:
    config = KworkConfig(
        token="worker",
        state_dir=Path(state_dir),
        persist_token=False,
        rps_limit=100.0,
        burst_limit=100,
        route_rps_limit=100.0,
        route_burst_limit=100,
        rate_wait_timeout=2.0,
        retry_backoff_base=0.0,
        retry_backoff_max=0.0,
        reconciliation_min_age_seconds=1.0,
    )
    store = CoordinationStore(config)

    async def hold() -> None:
        async with store.writer_guard("account-42"):
            ready.put(True)
            release.get(timeout=5)

    asyncio.run(hold())


@pytest.mark.asyncio
async def test_writer_guard_is_process_shared(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory()
    store = CoordinationStore(config)
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    release = context.Queue()
    process = context.Process(
        target=_writer_guard_worker,
        args=(str(config.state_dir), ready, release),
    )
    process.start()
    assert ready.get(timeout=5) is True
    try:
        with pytest.raises(GatewayError) as busy:
            async with store.writer_guard("account-42"):
                pytest.fail("second process must not enter writer guard")
        assert busy.value.code is ErrorCode.WRITE_IN_PROGRESS
    finally:
        release.put(True)
        process.join(timeout=5)
    assert process.exitcode == 0


def test_rate_limit_is_shared_between_processes(tmp_path: Path) -> None:
    state_dir = tmp_path / "shared"
    state_dir.mkdir(mode=0o700)
    os.chmod(state_dir, 0o700)
    # Initialize before forking to eliminate schema-creation timing from the assertion.
    CoordinationStore(
        KworkConfig(
            token="parent-token",
            state_dir=state_dir,
            persist_token=False,
            rps_limit=2.0,
            burst_limit=1,
            route_rps_limit=100.0,
            route_burst_limit=10,
        )
    )
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    processes = [context.Process(target=_rate_worker, args=(str(state_dir), barrier, results)) for _ in range(2)]
    for process in processes:
        process.start()
    timestamps = sorted(results.get(timeout=5) for _ in processes)
    for process in processes:
        process.join(timeout=5)
        assert process.exitcode == 0
    assert timestamps[1] - timestamps[0] >= 0.40
    assert stat.S_IMODE((state_dir / "coordination.sqlite3").stat().st_mode) == 0o600


def test_sqlite_connections_do_not_emit_resource_warnings(
    config_factory: Callable[..., KworkConfig],
) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", ResourceWarning)
        store = CoordinationStore(config_factory())
        with closing(store._connect()) as connection:
            assert isinstance(connection, sqlite3.Connection)
        del connection
        gc.collect()


@pytest.mark.parametrize(
    ("paging", "count", "expected"),
    [
        ({"page": 2, "limit": 20, "total": 41}, 20, (2, 20, 41, 3)),
        ({"page": 1, "pages": 0}, 0, (1, None, None, 0)),
        ({}, 7, (1, 7, None, None)),
    ],
)
def test_pages_from_paging(
    paging: dict[str, Any],
    count: int,
    expected: tuple[int, int | None, int | None, int | None],
) -> None:
    assert pages_from_paging(paging, count) == expected
