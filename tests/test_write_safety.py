"""Write-ledger guarantees: never claim a certain outcome without evidence."""

from __future__ import annotations

import hashlib
import io
import json
import os
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from kwork.schema.actor import Actor

from kwork_mcp.bootstrap import run_bootstrap_cli
from kwork_mcp.config import KworkConfig
from kwork_mcp.coordination import CoordinationStore, StoredWrite
from kwork_mcp.errors import AmbiguousWriteError, ContractDriftError, GatewayError
from kwork_mcp.gateway import KworkGateway
from kwork_mcp.models import (
    ConnectsData,
    DialogRecord,
    EditMessageRequest,
    ErrorCode,
    ItemCollection,
    KworkRecord,
    MessageRecord,
    OfferRecord,
    OrderRecord,
    ProjectRecord,
    SetKworkStateRequest,
    SubmitOfferRequest,
    WriteAction,
    WriteState,
)

ACCOUNT_ID = 42
SCOPE = f"account-{ACCOUNT_ID}"


class CookieJar:
    def filter_cookies(self, _url: Any) -> dict[str, Any]:
        return {}


# Built from parts so secret scanners do not mistake the fixture for a token.
_OFFER_PAGE_HTML = "".join(('csrf_user_token="', "abcdef0123456789", '" draftKey="draft123"'))


class OfferWeb:
    base_url = "https://kwork.ru/"

    def __init__(self, *, page_status: int = 200, final: dict[str, Any] | None = None) -> None:
        self.page_status = page_status
        self.final = final or {"status": 200, "json": {"success": True, "id": 901}}
        self.final_sent = False
        self.calls: list[str] = []

    async def open_new_offer_page(self, **_params: Any) -> dict[str, Any]:
        self.calls.append("page")
        return {"status": self.page_status, "text": _OFFER_PAGE_HTML}

    async def quick_faq_init(self, **_params: Any) -> dict[str, Any]:
        self.calls.append("faq")
        return {"status": 200}

    async def create_offer_draft(self, **_params: Any) -> dict[str, Any]:
        self.calls.append("draft")
        return {"status": 200}

    async def check_is_template(self, **_params: Any) -> dict[str, Any]:
        self.calls.append("template")
        return {"status": 200}

    async def create_exchange_offer(self, **_params: Any) -> dict[str, Any]:
        self.calls.append("final")
        self.final_sent = True
        return self.final


class SafetySession:
    scope = SCOPE

    def __init__(self, web: OfferWeb | None = None) -> None:
        self.web_client = SimpleNamespace(web=web or OfferWeb(), session=SimpleNamespace(cookie_jar=CookieJar()))
        self.web_invalidations = 0
        self.remote_steps: list[tuple[str, bool]] = []
        self.locally_throttled_routes: set[str] = set()

    @asynccontextmanager
    async def exclusive_client(self) -> Any:
        yield

    async def verify_account_identity(self) -> Actor:
        return Actor(id=ACCOUNT_ID, username="fixture")

    async def verify_write_identity(self) -> Actor:
        return Actor(id=ACCOUNT_ID, username="fixture")

    async def call_read(self, _route: str, operation: Callable[[Any], Awaitable[Any]]) -> Any:
        return await operation(SimpleNamespace(get_me=self.verify_account_identity))

    async def ensure_web_client(self) -> Any:
        return self.web_client

    def invalidate_web_login(self) -> None:
        self.web_invalidations += 1

    async def call_write_step(
        self,
        route: str,
        operation: Callable[[Any], Awaitable[Any]],
        *,
        before_remote_attempt: Callable[[], Awaitable[None]] | None = None,
    ) -> Any:
        self.remote_steps.append((route, before_remote_attempt is not None))
        if route in self.locally_throttled_routes:
            # The shared limiter refuses before the boundary, as in the session.
            raise GatewayError(ErrorCode.RATE_LIMIT, retryable=True, safe_to_retry=True, diagnostic="local")
        if before_remote_attempt is not None:
            await before_remote_attempt()
        return await operation(self.web_client)


class SafetyGateway(KworkGateway):
    """Real write protocol and handlers over in-memory Kwork state."""

    def __init__(self, config: KworkConfig, session: SafetySession | None = None) -> None:
        super().__init__(config, CoordinationStore(config), session or SafetySession())  # type: ignore[arg-type]
        self.offers: list[OfferRecord] = []
        self.offer_readback_error: GatewayError | None = None
        self.messages: dict[int, MessageRecord] = {}
        self.message_lookup_error: GatewayError | None = None
        self.message_lookups = 0
        self.dialogs: list[DialogRecord] = []
        self.orders: list[OrderRecord] = []
        self.kworks: list[KworkRecord] = []
        self.remote_error: GatewayError | None = None
        self.remote_calls = 0

    @property
    def web(self) -> OfferWeb:
        return self.session.web_client.web  # type: ignore[attr-defined,no-any-return]

    async def get_project(self, project_id: int) -> ProjectRecord:
        return ProjectRecord(project_id=project_id, status="active", raw={"id": project_id})

    async def get_connects(self) -> ConnectsData:
        return ConnectsData(active=5, total=10, raw={"active": 5, "total": 10})

    async def _all_offers(self, max_pages: int = 50) -> list[OfferRecord]:
        if self.web.final_sent and self.offer_readback_error is not None:
            raise self.offer_readback_error
        return list(self.offers)

    async def _find_message(self, *, username: str, message_id: int, max_pages: int = 50) -> MessageRecord | None:
        self.message_lookups += 1
        if self.message_lookup_error is not None and self.message_lookups > 1:
            raise self.message_lookup_error
        return self.messages.get(message_id)

    async def _find_dialog_by_user_id(self, user_id: int, *, max_pages: int = 50) -> DialogRecord | None:
        return next((dialog for dialog in self.dialogs if dialog.user_id == user_id), None)

    async def _all_orders(self, max_pages: int = 50) -> list[OrderRecord]:
        return list(self.orders)

    async def list_my_kworks(self) -> ItemCollection[KworkRecord]:
        return ItemCollection[KworkRecord](items=list(self.kworks))


class TimeoutGateway(SafetyGateway):
    async def _execute_write(
        self,
        record: StoredWrite,
        *,
        before_remote_attempt: Callable[[], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        assert before_remote_attempt is not None
        await before_remote_attempt()
        self.remote_calls += 1
        if self.remote_calls == 1:
            raise GatewayError(ErrorCode.TIMEOUT, retryable=True, safe_to_retry=True, diagnostic="lost")
        return {"offer_id": 900 + self.remote_calls, "project_id": 1}


def _offer(project_id: int = 77) -> SubmitOfferRequest:
    return SubmitOfferRequest(
        action=WriteAction.SUBMIT_OFFER,
        project_id=project_id,
        title="Реализация API",
        description="Д" * 180,
        price=5000,
        duration_days=7,
    )


def _writes_config(config_factory: Callable[..., KworkConfig]) -> KworkConfig:
    return config_factory(enable_writes=True, expected_user_id=ACCOUNT_ID)


async def _prepare_and_commit(gateway: KworkGateway, request: Any, idempotency: str) -> Any:
    prepared = await gateway.prepare_write(request, idempotency, correlation_id="prepare")
    assert prepared.confirmation_token is not None
    return await gateway.commit_write(
        write_id=prepared.write_id,
        payload_hash=prepared.payload_hash,
        confirmation_token=prepared.confirmation_token,
        correlation_id="commit",
    )


def _record(action: WriteAction) -> StoredWrite:
    now = time.time()
    return StoredWrite(
        write_id="00000000-0000-0000-0000-000000000000",
        scope=SCOPE,
        idempotency_key="fixture-key",
        action=action,
        payload_json="{}",
        payload_hash="0" * 64,
        state=WriteState.SUBMISSION_UNKNOWN,
        prepared_at=now - 60,
        expires_at=now + 600,
        updated_at=now - 30,
        lease_owner=None,
        lease_expires=None,
        remote_started_at=now - 45,
        result_json=None,
        error_json=None,
    )


# --- submit_offer: a created offer is never reported as a safe-to-retry failure


@pytest.mark.asyncio
async def test_offer_created_without_id_and_failed_lookup_stays_unknown(
    config_factory: Callable[..., KworkConfig],
) -> None:
    gateway = SafetyGateway(
        _writes_config(config_factory),
        SafetySession(OfferWeb(final={"status": 200, "json": {"success": True}})),
    )
    gateway.offer_readback_error = GatewayError(
        ErrorCode.RATE_LIMIT,
        retryable=True,
        safe_to_retry=True,
        diagnostic="readback_throttled",
    )

    status = await _prepare_and_commit(gateway, _offer(), "offer-created-lookup-throttled")

    assert gateway.web.final_sent is True
    assert status.state is WriteState.SUBMISSION_UNKNOWN
    assert status.terminal_error is not None
    assert status.terminal_error.code is ErrorCode.AMBIGUOUS_WRITE
    assert status.terminal_error.safe_to_retry is False


@pytest.mark.asyncio
async def test_offer_page_csrf_failure_keeps_write_committable_and_relogs(
    config_factory: Callable[..., KworkConfig],
) -> None:
    session = SafetySession(OfferWeb(page_status=403))
    gateway = SafetyGateway(_writes_config(config_factory), session)
    prepared = await gateway.prepare_write(_offer(), "offer-page-csrf", correlation_id="prepare")
    assert prepared.confirmation_token is not None

    with pytest.raises(GatewayError) as failure:
        await gateway.commit_write(
            write_id=prepared.write_id,
            payload_hash=prepared.payload_hash,
            confirmation_token=prepared.confirmation_token,
            correlation_id="commit-1",
        )
    assert failure.value.code is ErrorCode.CSRF
    assert session.web_invalidations == 1
    assert gateway.web.final_sent is False
    after_failure = await gateway.get_write_status(prepared.write_id, correlation_id="status")
    assert after_failure is not None
    assert after_failure.state is WriteState.PREPARED

    gateway.web.page_status = 200
    committed = await gateway.commit_write(
        write_id=prepared.write_id,
        payload_hash=prepared.payload_hash,
        confirmation_token=prepared.confirmation_token,
        correlation_id="commit-2",
    )
    assert committed.state is WriteState.SUCCEEDED


@pytest.mark.asyncio
async def test_offer_remote_boundary_is_the_final_create_step(
    config_factory: Callable[..., KworkConfig],
) -> None:
    session = SafetySession()
    gateway = SafetyGateway(_writes_config(config_factory), session)

    committed = await _prepare_and_commit(gateway, _offer(), "offer-boundary")

    assert committed.state is WriteState.SUCCEEDED
    assert [route for route, crosses in session.remote_steps if crosses] == ["write-offer-final"]


@pytest.mark.asyncio
async def test_local_throttle_before_final_create_is_retryable_not_ambiguous(
    config_factory: Callable[..., KworkConfig],
) -> None:
    session = SafetySession()
    session.locally_throttled_routes.add("write-offer-final")
    gateway = SafetyGateway(_writes_config(config_factory), session)
    prepared = await gateway.prepare_write(_offer(), "offer-local-throttle", correlation_id="prepare")
    assert prepared.confirmation_token is not None

    with pytest.raises(GatewayError) as failure:
        await gateway.commit_write(
            write_id=prepared.write_id,
            payload_hash=prepared.payload_hash,
            confirmation_token=prepared.confirmation_token,
            correlation_id="commit",
        )

    assert failure.value.code is ErrorCode.RATE_LIMIT
    assert failure.value.reconciliation_required is False
    status = await gateway.get_write_status(prepared.write_id, correlation_id="status")
    assert status is not None
    assert status.state is WriteState.PREPARED


# --- account write barrier: always identifiable and resolvable


@pytest.mark.asyncio
async def test_write_barrier_names_the_unresolved_write(
    config_factory: Callable[..., KworkConfig],
) -> None:
    gateway = TimeoutGateway(_writes_config(config_factory))
    unknown = await _prepare_and_commit(gateway, _offer(77), "first-offer")
    assert unknown.state is WriteState.SUBMISSION_UNKNOWN

    with pytest.raises(GatewayError) as blocked:
        await _prepare_and_commit(gateway, _offer(78), "second-offer")

    assert blocked.value.code is ErrorCode.AMBIGUOUS_WRITE
    assert blocked.value.related_write_id == unknown.write_id
    assert blocked.value.to_info("corr").related_write_id == unknown.write_id


@pytest.mark.asyncio
async def test_account_status_lists_unresolved_writes(
    config_factory: Callable[..., KworkConfig],
) -> None:
    gateway = TimeoutGateway(_writes_config(config_factory))
    unknown = await _prepare_and_commit(gateway, _offer(), "status-listing")

    account = await gateway.account_status()

    assert account.unresolved_write_ids == [unknown.write_id]


@pytest.mark.asyncio
async def test_operator_resolution_lifts_the_barrier(
    config_factory: Callable[..., KworkConfig],
) -> None:
    gateway = TimeoutGateway(_writes_config(config_factory))
    unknown = await _prepare_and_commit(gateway, _offer(77), "operator-first")

    resolved = await gateway.coordinator.operator_resolve_write(
        write_id=unknown.write_id,
        scope=SCOPE,
        state=WriteState.RECONCILED_ABSENT,
    )
    assert resolved.state is WriteState.RECONCILED_ABSENT

    second = await _prepare_and_commit(gateway, _offer(78), "operator-second")
    assert second.state is WriteState.SUCCEEDED


@pytest.mark.asyncio
async def test_operator_resolution_only_applies_to_unknown_writes(
    config_factory: Callable[..., KworkConfig],
) -> None:
    gateway = SafetyGateway(_writes_config(config_factory))
    prepared = await gateway.prepare_write(_offer(), "operator-prepared", correlation_id="prepare")

    with pytest.raises(GatewayError) as rejected:
        await gateway.coordinator.operator_resolve_write(
            write_id=prepared.write_id,
            scope=SCOPE,
            state=WriteState.RECONCILED_SUCCEEDED,
        )
    assert rejected.value.code is ErrorCode.VALIDATION


def _operator_environment(monkeypatch: pytest.MonkeyPatch, config: KworkConfig) -> None:
    """The operator CLI must run with the same shared policy as the server."""

    monkeypatch.setenv("KWORK_EXPECTED_USER_ID", str(ACCOUNT_ID))
    monkeypatch.setenv("KWORK_STATE_DIR", str(config.state_dir))
    for name in ("rps_limit", "burst_limit", "route_rps_limit", "route_burst_limit", "reconciliation_min_age_seconds"):
        monkeypatch.setenv(f"KWORK_{name.upper()}", str(getattr(config, name)))


class TTYBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.mark.asyncio
async def test_bootstrap_cli_lists_and_resolves_unknown_writes(
    config_factory: Callable[..., KworkConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _writes_config(config_factory)
    gateway = TimeoutGateway(config)
    unknown = await _prepare_and_commit(gateway, _offer(), "cli-resolution")
    _operator_environment(monkeypatch, config)

    listed = io.StringIO()
    code = await run_bootstrap_cli(
        ["pending-writes"],
        stdin=TTYBuffer(),
        stdout=listed,
        stderr=TTYBuffer(),
    )
    assert code == 0
    pending = json.loads(listed.getvalue())
    assert [item["write_id"] for item in pending["writes"]] == [unknown.write_id]
    assert pending["writes"][0]["action"] == "submit_offer"

    resolved_out = io.StringIO()
    code = await run_bootstrap_cli(
        ["resolve-write", unknown.write_id, "absent"],
        stdin=TTYBuffer("да\n"),
        stdout=resolved_out,
        stderr=TTYBuffer(),
    )
    assert code == 0
    assert json.loads(resolved_out.getvalue())["state"] == "reconciled_absent"
    record = await gateway.coordinator.get_write(unknown.write_id, scope=SCOPE)
    assert record is not None
    assert record.state is WriteState.RECONCILED_ABSENT


@pytest.mark.asyncio
async def test_bootstrap_cli_refuses_a_missing_ledger_instead_of_creating_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "not-the-server-state"
    monkeypatch.setenv("KWORK_EXPECTED_USER_ID", str(ACCOUNT_ID))
    monkeypatch.setenv("KWORK_STATE_DIR", str(state_dir))

    code = await run_bootstrap_cli(
        ["pending-writes"],
        stdin=TTYBuffer(),
        stdout=(stdout := io.StringIO()),
        stderr=(stderr := TTYBuffer()),
    )

    assert code == 1
    assert stdout.getvalue() == ""
    assert "KWORK_STATE_DIR" in stderr.getvalue()
    assert not (state_dir / "coordination.sqlite3").exists()


@pytest.mark.asyncio
async def test_bootstrap_cli_escapes_terminal_controls_in_write_summaries(
    config_factory: Callable[..., KworkConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _writes_config(config_factory)
    gateway = TimeoutGateway(config)
    request = _offer().model_copy(update={"title": "Отзыв\u202eтекст\u009b"})
    unknown = await _prepare_and_commit(gateway, request, "cli-controls")
    _operator_environment(monkeypatch, config)

    code = await run_bootstrap_cli(
        ["pending-writes"], stdin=TTYBuffer(), stdout=(listed := io.StringIO()), stderr=TTYBuffer()
    )
    assert code == 0
    code = await run_bootstrap_cli(
        ["resolve-write", unknown.write_id, "absent"],
        stdin=TTYBuffer("нет\n"),
        stdout=io.StringIO(),
        stderr=(summary := TTYBuffer()),
    )
    assert code == 1

    for rendered in (listed.getvalue(), summary.getvalue()):
        assert "\u202e" not in rendered
        assert "\u009b" not in rendered
        assert "\\u202e" in rendered
        assert "Отзыв" in rendered
    assert json.loads(listed.getvalue())["writes"][0]["request"]["title"] == "Отзыв\u202eтекст\u009b"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("argv", "stdin_text", "tty", "expected_code", "expected_message"),
    [
        (["pending-writes", "extra"], "", True, 2, "не принимает аргументы"),
        (["resolve-write", "only-id"], "", True, 2, "Использование"),
        (["resolve-write", "WRITE", "maybe"], "", True, 2, "Использование"),
        (["resolve-write", "WRITE", "absent"], "", False, 2, "TTY"),
        (["resolve-write", "00000000-0000-0000-0000-00000000dead", "absent"], "", True, 1, "not_found"),
        (["resolve-write", "WRITE", "absent"], "", True, 130, "не изменена"),
    ],
)
async def test_bootstrap_cli_write_admin_rejects_bad_usage(
    config_factory: Callable[..., KworkConfig],
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    stdin_text: str,
    tty: bool,
    expected_code: int,
    expected_message: str,
) -> None:
    config = _writes_config(config_factory)
    gateway = TimeoutGateway(config)
    unknown = await _prepare_and_commit(gateway, _offer(), "cli-usage")
    _operator_environment(monkeypatch, config)
    stream_type = TTYBuffer if tty else io.StringIO

    code = await run_bootstrap_cli(
        [unknown.write_id if item == "WRITE" else item for item in argv],
        stdin=stream_type(stdin_text),
        stdout=(stdout := io.StringIO()),
        stderr=(stderr := stream_type()),
    )

    assert code == expected_code
    assert expected_message in stderr.getvalue()
    assert stdout.getvalue() == ""
    record = await gateway.coordinator.get_write(unknown.write_id, scope=SCOPE)
    assert record is not None
    assert record.state is WriteState.SUBMISSION_UNKNOWN


@pytest.mark.asyncio
async def test_bootstrap_cli_does_not_resolve_a_settled_write(
    config_factory: Callable[..., KworkConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _writes_config(config_factory)
    gateway = SafetyGateway(config)
    prepared = await gateway.prepare_write(_offer(), "cli-settled", correlation_id="prepare")
    _operator_environment(monkeypatch, config)

    code = await run_bootstrap_cli(
        ["resolve-write", prepared.write_id, "succeeded"],
        stdin=TTYBuffer("да\n"),
        stdout=io.StringIO(),
        stderr=(stderr := TTYBuffer()),
    )

    assert code == 1
    assert "prepared" in stderr.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("env", "expected_code", "expected_message"),
    [
        ({"KWORK_STATE_DIR": "relative/state"}, 2, "KWORK_STATE_DIR"),
        ({}, 2, "KWORK_EXPECTED_USER_ID"),
    ],
)
async def test_bootstrap_cli_write_admin_requires_valid_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    expected_code: int,
    expected_message: str,
) -> None:
    for name in [name for name in os.environ if name.upper().startswith("KWORK_")]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KWORK_STATE_DIR", str(tmp_path))
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    code = await run_bootstrap_cli(
        ["pending-writes"],
        stdin=TTYBuffer(),
        stdout=io.StringIO(),
        stderr=(stderr := TTYBuffer()),
    )

    assert code == expected_code
    assert expected_message in stderr.getvalue()


@pytest.mark.asyncio
async def test_bootstrap_cli_resolution_requires_explicit_confirmation(
    config_factory: Callable[..., KworkConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _writes_config(config_factory)
    gateway = TimeoutGateway(config)
    unknown = await _prepare_and_commit(gateway, _offer(), "cli-declined")
    _operator_environment(monkeypatch, config)

    code = await run_bootstrap_cli(
        ["resolve-write", unknown.write_id, "succeeded"],
        stdin=TTYBuffer("нет\n"),
        stdout=io.StringIO(),
        stderr=TTYBuffer(),
    )

    assert code == 1
    record = await gateway.coordinator.get_write(unknown.write_id, scope=SCOPE)
    assert record is not None
    assert record.state is WriteState.SUBMISSION_UNKNOWN


# --- reconciliation: unknown states are never negative evidence


def _edit_request() -> EditMessageRequest:
    return EditMessageRequest(
        action=WriteAction.EDIT_MESSAGE,
        message_id=5,
        username="client",
        text="Новый текст",
    )


def _message(text: str | None) -> MessageRecord:
    return MessageRecord(message_id=5, sender_id=ACCOUNT_ID, text=text, raw={"message_id": 5})


@pytest.mark.asyncio
async def test_edit_preflight_fingerprints_the_original_text(
    config_factory: Callable[..., KworkConfig],
) -> None:
    gateway = SafetyGateway(_writes_config(config_factory))
    gateway.messages[5] = _message("Старый текст")
    resolved: dict[str, Any] = {}

    await gateway._preflight(_edit_request(), resolved)

    assert resolved["message_text_sha256_at_prepare"] == hashlib.sha256("Старый текст".encode()).hexdigest()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current_text", "expected"),
    [("Новый текст", True), ("Старый текст", False)],
)
async def test_edit_read_back_distinguishes_applied_from_unchanged(
    config_factory: Callable[..., KworkConfig],
    current_text: str,
    expected: bool,
) -> None:
    gateway = SafetyGateway(_writes_config(config_factory))
    gateway.messages[5] = _message(current_text)
    resolved = {"message_text_sha256_at_prepare": hashlib.sha256("Старый текст".encode()).hexdigest()}

    present, _result = await gateway._read_back(
        WriteAction.EDIT_MESSAGE,
        _edit_request().model_dump(mode="json"),
        resolved,
        _record(WriteAction.EDIT_MESSAGE),
    )

    assert present is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("message_text", [None, "Третий вариант"])
async def test_edit_read_back_is_ambiguous_for_missing_or_foreign_text(
    config_factory: Callable[..., KworkConfig],
    message_text: str | None,
) -> None:
    gateway = SafetyGateway(_writes_config(config_factory))
    if message_text is not None:
        gateway.messages[5] = _message(message_text)
    resolved = {"message_text_sha256_at_prepare": hashlib.sha256("Старый текст".encode()).hexdigest()}

    with pytest.raises(AmbiguousWriteError):
        await gateway._read_back(
            WriteAction.EDIT_MESSAGE,
            _edit_request().model_dump(mode="json"),
            resolved,
            _record(WriteAction.EDIT_MESSAGE),
        )


def _kwork(group_id: int, group_name: str) -> KworkRecord:
    return KworkRecord(kwork_id=7, status_group_id=group_id, status_group_name=group_name, raw={"id": 7})


@pytest.mark.asyncio
async def test_set_kwork_state_preflight_records_the_current_group(
    config_factory: Callable[..., KworkConfig],
) -> None:
    gateway = SafetyGateway(_writes_config(config_factory))
    gateway.kworks = [_kwork(3, "Остановленные")]
    resolved: dict[str, Any] = {}

    await gateway._preflight(
        SetKworkStateRequest(action=WriteAction.SET_KWORK_STATE, kwork_id=7, target_state="active"),
        resolved,
    )

    assert resolved["kwork_status_group_id_at_prepare"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kworks", "resolved", "expected"),
    [
        ([_kwork(7, "Активные")], {"kwork_status_group_id_at_prepare": 3}, True),
        ([_kwork(3, "Остановленные")], {"kwork_status_group_id_at_prepare": 3}, False),
        ([_kwork(4, "На паузе")], {}, False),
    ],
)
async def test_set_kwork_state_read_back_uses_group_evidence(
    config_factory: Callable[..., KworkConfig],
    kworks: list[KworkRecord],
    resolved: dict[str, Any],
    expected: bool,
) -> None:
    gateway = SafetyGateway(_writes_config(config_factory))
    gateway.kworks = kworks

    present, _result = await gateway._read_back(
        WriteAction.SET_KWORK_STATE,
        {"action": "set_kwork_state", "kwork_id": 7, "target_state": "active"},
        resolved,
        _record(WriteAction.SET_KWORK_STATE),
    )

    assert present is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kworks", "resolved"),
    [
        ([], {"kwork_status_group_id_at_prepare": 3}),
        ([_kwork(1, "На модерации")], {"kwork_status_group_id_at_prepare": 3}),
        ([_kwork(1, "На модерации")], {}),
    ],
)
async def test_set_kwork_state_read_back_is_ambiguous_for_other_groups(
    config_factory: Callable[..., KworkConfig],
    kworks: list[KworkRecord],
    resolved: dict[str, Any],
) -> None:
    gateway = SafetyGateway(_writes_config(config_factory))
    gateway.kworks = kworks

    with pytest.raises(AmbiguousWriteError):
        await gateway._read_back(
            WriteAction.SET_KWORK_STATE,
            {"action": "set_kwork_state", "kwork_id": 7, "target_state": "active"},
            resolved,
            _record(WriteAction.SET_KWORK_STATE),
        )


@pytest.mark.asyncio
async def test_inactive_group_name_is_not_mistaken_for_active(
    config_factory: Callable[..., KworkConfig],
) -> None:
    gateway = SafetyGateway(_writes_config(config_factory))
    gateway.kworks = [_kwork(9, "Неактивные")]

    with pytest.raises(AmbiguousWriteError):
        await gateway._read_back(
            WriteAction.SET_KWORK_STATE,
            {"action": "set_kwork_state", "kwork_id": 7, "target_state": "active"},
            {"kwork_status_group_id_at_prepare": 3},
            _record(WriteAction.SET_KWORK_STATE),
        )


@pytest.mark.asyncio
async def test_missing_order_is_not_evidence_of_absent_approval(
    config_factory: Callable[..., KworkConfig],
) -> None:
    gateway = SafetyGateway(_writes_config(config_factory))

    with pytest.raises(AmbiguousWriteError):
        await gateway._read_back(
            WriteAction.SUBMIT_ORDER_APPROVAL,
            {"action": "submit_order_approval", "order_id": 81},
            {},
            _record(WriteAction.SUBMIT_ORDER_APPROVAL),
        )


@pytest.mark.asyncio
async def test_missing_dialog_is_not_evidence_of_unread_state(
    config_factory: Callable[..., KworkConfig],
) -> None:
    gateway = SafetyGateway(_writes_config(config_factory))

    with pytest.raises(AmbiguousWriteError):
        await gateway._read_back(
            WriteAction.MARK_DIALOG_READ,
            {"action": "mark_dialog_read", "user_id": 99},
            {},
            _record(WriteAction.MARK_DIALOG_READ),
        )


# --- preflight that cannot conclude never burns or blocks a write


@pytest.mark.asyncio
async def test_inconclusive_commit_preflight_returns_write_to_prepared(
    config_factory: Callable[..., KworkConfig],
) -> None:
    gateway = SafetyGateway(_writes_config(config_factory))
    gateway.messages[5] = _message("Старый текст")
    gateway.message_lookup_error = AmbiguousWriteError("message_lookup_missing_stable_id")
    prepared = await gateway.prepare_write(_edit_request(), "edit-inconclusive", correlation_id="prepare")
    assert prepared.confirmation_token is not None

    with pytest.raises(GatewayError) as failure:
        await gateway.commit_write(
            write_id=prepared.write_id,
            payload_hash=prepared.payload_hash,
            confirmation_token=prepared.confirmation_token,
            correlation_id="commit",
        )

    assert failure.value.code is ErrorCode.UPSTREAM_UNAVAILABLE
    assert failure.value.retryable is True
    assert failure.value.reconciliation_required is False
    status = await gateway.get_write_status(prepared.write_id, correlation_id="status")
    assert status is not None
    assert status.state is WriteState.PREPARED
    assert status.can_commit is True


@pytest.mark.asyncio
async def test_inconclusive_commit_preflight_on_expired_write_reports_expiry(
    config_factory: Callable[..., KworkConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _writes_config(config_factory)
    gateway = SafetyGateway(config)
    gateway.messages[5] = _message("Старый текст")
    prepared = await gateway.prepare_write(_edit_request(), "edit-expires", correlation_id="prepare")
    assert prepared.confirmation_token is not None
    clock = {"now": time.time()}
    monkeypatch.setattr("kwork_mcp.coordination.time.time", lambda: clock["now"])

    async def lookup_after_ttl(**_kwargs: Any) -> MessageRecord | None:
        clock["now"] += config.preparation_ttl_seconds + 1
        raise AmbiguousWriteError("message_lookup_missing_stable_id")

    gateway._find_message = lookup_after_ttl  # type: ignore[method-assign]

    status = await gateway.commit_write(
        write_id=prepared.write_id,
        payload_hash=prepared.payload_hash,
        confirmation_token=prepared.confirmation_token,
        correlation_id="commit",
    )

    assert status.state is WriteState.EXPIRED
    assert status.can_commit is False


@pytest.mark.asyncio
async def test_contract_drift_in_commit_preflight_returns_write_to_prepared(
    config_factory: Callable[..., KworkConfig],
) -> None:
    gateway = SafetyGateway(_writes_config(config_factory))
    gateway.messages[5] = _message("Старый текст")
    gateway.message_lookup_error = ContractDriftError("dialog:pagination_safety_limit")
    prepared = await gateway.prepare_write(_edit_request(), "edit-drift", correlation_id="prepare")
    assert prepared.confirmation_token is not None

    with pytest.raises(GatewayError) as failure:
        await gateway.commit_write(
            write_id=prepared.write_id,
            payload_hash=prepared.payload_hash,
            confirmation_token=prepared.confirmation_token,
            correlation_id="commit",
        )

    assert failure.value.code is ErrorCode.CONTRACT_DRIFT
    status = await gateway.get_write_status(prepared.write_id, correlation_id="status")
    assert status is not None
    assert status.state is WriteState.PREPARED


@pytest.mark.asyncio
async def test_inconclusive_prepare_preflight_is_retryable_not_reconcilable(
    config_factory: Callable[..., KworkConfig],
) -> None:
    gateway = SafetyGateway(_writes_config(config_factory))
    gateway.message_lookup_error = AmbiguousWriteError("message_lookup_missing_stable_id")
    gateway.message_lookups = 1

    with pytest.raises(GatewayError) as failure:
        await gateway.prepare_write(_edit_request(), "edit-prepare-inconclusive", correlation_id="prepare")

    assert failure.value.code is ErrorCode.UPSTREAM_UNAVAILABLE
    assert failure.value.retryable is True
    assert failure.value.reconciliation_required is False
