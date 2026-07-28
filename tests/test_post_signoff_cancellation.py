from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from kwork.schema.actor import Actor

from kwork_mcp.config import KworkConfig
from kwork_mcp.coordination import CoordinationStore, StoredWrite
from kwork_mcp.errors import AmbiguousWriteError, GatewayError
from kwork_mcp.gateway import KworkGateway, _finish_shielded_task
from kwork_mcp.models import ErrorCode, WriteAction, WriteState


class CancellationSession:
    scope = "account-42"

    def __init__(self, stage: str | None = None) -> None:
        self.stage = stage
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def verify_write_identity(self) -> Actor:
        if self.stage == "identity":
            self.entered.set()
            await self.release.wait()
        return Actor(id=42, username="fixture")  # type: ignore[call-arg]

    @asynccontextmanager
    async def exclusive_client(self) -> AsyncIterator[None]:
        if self.stage == "exclusive":
            self.entered.set()
            await self.release.wait()
        yield


class CancellationGateway(KworkGateway):
    def __init__(
        self,
        config: KworkConfig,
        coordinator: CoordinationStore,
        *,
        stage: str | None = None,
    ) -> None:
        self.test_session = CancellationSession(stage)
        super().__init__(
            config,
            coordinator,
            self.test_session,  # type: ignore[arg-type]
        )
        self.stage = stage
        self.preflight_entered = asyncio.Event()
        self.preflight_release = asyncio.Event()
        self.before_remote_entered = asyncio.Event()
        self.before_remote_release = asyncio.Event()
        self.remote_entered = asyncio.Event()
        self.remote_release = asyncio.Event()
        self.remote_calls = 0

    async def _preflight(self, request: Any, resolved: dict[str, Any]) -> None:
        if self.stage == "preflight":
            self.preflight_entered.set()
            await self.preflight_release.wait()

    async def _execute_write(
        self,
        record: StoredWrite,
        *,
        before_remote_attempt: Callable[[], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        if self.stage == "before_remote":
            self.before_remote_entered.set()
            await self.before_remote_release.wait()
        assert before_remote_attempt is not None
        await before_remote_attempt()
        self.remote_calls += 1
        self.remote_entered.set()
        if self.stage == "remote":
            await self.remote_release.wait()
        return {"user_id": 7, "read": True}

    def stage_entered(self) -> asyncio.Event:
        if self.stage in {"identity", "exclusive"}:
            return self.test_session.entered
        if self.stage == "preflight":
            return self.preflight_entered
        if self.stage == "before_remote":
            return self.before_remote_entered
        if self.stage == "remote":
            return self.remote_entered
        raise AssertionError(f"stage has no wait point: {self.stage}")


class AdmissionFailureSession(CancellationSession):
    def __init__(self, failure_point: str) -> None:
        super().__init__()
        self.failure_point = failure_point

    async def ensure_web_client(self) -> Any:
        if self.failure_point == "web":
            raise GatewayError(
                ErrorCode.TIMEOUT,
                retryable=True,
                safe_to_retry=True,
                diagnostic="web_admission_timeout",
            )
        raise AssertionError("unexpected ensure_web_client call")

    async def call_write_step(self, *_args: Any, **_kwargs: Any) -> Any:
        if self.failure_point == "rate":
            raise GatewayError(
                ErrorCode.CIRCUIT_OPEN,
                retryable=True,
                safe_to_retry=True,
                diagnostic="rate_admission_closed",
            )
        raise AssertionError("unexpected call_write_step call")


class AdmissionFailureGateway(KworkGateway):
    def __init__(
        self,
        config: KworkConfig,
        coordinator: CoordinationStore,
        failure_point: str,
    ) -> None:
        super().__init__(
            config,
            coordinator,
            AdmissionFailureSession(failure_point),  # type: ignore[arg-type]
        )

    async def _preflight(self, request: Any, resolved: dict[str, Any]) -> None:
        return None


class NoBoundaryGateway(CancellationGateway):
    async def _execute_write(
        self,
        record: StoredWrite,
        *,
        before_remote_attempt: Callable[[], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        return {"impossible": True}


class ImmediateFailureGateway(CancellationGateway):
    def __init__(
        self,
        config: KworkConfig,
        coordinator: CoordinationStore,
        failure: BaseException,
    ) -> None:
        super().__init__(config, coordinator)
        self.failure = failure

    async def _execute_write(
        self,
        record: StoredWrite,
        *,
        before_remote_attempt: Callable[[], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        raise self.failure


class DelayedClaimStore(CoordinationStore):
    def __init__(self, config: KworkConfig) -> None:
        self.claim_committed = asyncio.Event()
        self.claim_return = asyncio.Event()
        super().__init__(config)

    async def claim_write(self, **kwargs: Any) -> StoredWrite:
        claimed = await super().claim_write(**kwargs)
        self.claim_committed.set()
        await self.claim_return.wait()
        return claimed


class DelayedReleaseStore(CoordinationStore):
    def __init__(self, config: KworkConfig) -> None:
        self.release_entered = asyncio.Event()
        self.release_allowed = asyncio.Event()
        super().__init__(config)

    async def release_write_claim(self, **kwargs: Any) -> StoredWrite:
        self.release_entered.set()
        await self.release_allowed.wait()
        return await super().release_write_claim(**kwargs)


class DelayedFinishStore(CoordinationStore):
    def __init__(self, config: KworkConfig) -> None:
        self.finish_entered = asyncio.Event()
        self.finish_allowed = asyncio.Event()
        super().__init__(config)

    async def finish_write(self, **kwargs: Any) -> StoredWrite:
        if kwargs["state"] is WriteState.SUCCEEDED:
            self.finish_entered.set()
            await self.finish_allowed.wait()
        return await super().finish_write(**kwargs)


class DelayedMarkerStore(CoordinationStore):
    def __init__(self, config: KworkConfig) -> None:
        self.marker_committed = asyncio.Event()
        self.marker_return = asyncio.Event()
        super().__init__(config)

    async def mark_write_remote_started(self, **kwargs: Any) -> StoredWrite:
        marked = await super().mark_write_remote_started(**kwargs)
        self.marker_committed.set()
        await self.marker_return.wait()
        return marked


class MarkerCommitThenErrorStore(CoordinationStore):
    def __init__(self, config: KworkConfig, failure: BaseException) -> None:
        self.failure = failure
        super().__init__(config)

    async def mark_write_remote_started(self, **kwargs: Any) -> StoredWrite:
        await super().mark_write_remote_started(**kwargs)
        raise self.failure


class FailingReleaseStore(CoordinationStore):
    async def release_write_claim(self, **kwargs: Any) -> StoredWrite:
        raise RuntimeError("simulated-release-failure")


async def _prepare_record(
    store: CoordinationStore,
    *,
    key: str,
) -> tuple[StoredWrite, str]:
    prepared = await store.prepare_write(
        scope="account-42",
        idempotency_key=key,
        action=WriteAction.MARK_DIALOG_READ,
        payload={
            "request": {
                "action": WriteAction.MARK_DIALOG_READ.value,
                "user_id": 7,
            },
            "resolved": {},
            "prepared_account_id": 42,
        },
    )
    assert prepared.confirmation_token is not None
    return prepared.record, prepared.confirmation_token


async def _prepare_custom_record(
    store: CoordinationStore,
    *,
    key: str,
    action: WriteAction,
    request: dict[str, Any],
) -> tuple[StoredWrite, str]:
    prepared = await store.prepare_write(
        scope="account-42",
        idempotency_key=key,
        action=action,
        payload={
            "request": request,
            "resolved": {},
            "prepared_account_id": 42,
        },
    )
    assert prepared.confirmation_token is not None
    return prepared.record, prepared.confirmation_token


def _commit_task(
    gateway: KworkGateway,
    record: StoredWrite,
    token: str,
) -> asyncio.Task[Any]:
    return asyncio.create_task(
        gateway.commit_write(
            write_id=record.write_id,
            payload_hash=record.payload_hash,
            confirmation_token=token,
            correlation_id="cancel-test",
        )
    )


async def _assert_cancelled(task: asyncio.Task[Any]) -> None:
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_gateway_shield_helper_propagates_child_cancellation_without_spinning() -> None:
    async def cancelled_child() -> None:
        raise asyncio.CancelledError

    child = asyncio.create_task(cancelled_child())
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(_finish_shielded_task(child), timeout=0.5)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_point", "action", "request_payload", "error_code"),
    [
        (
            "rate",
            WriteAction.MARK_DIALOG_READ,
            {
                "action": WriteAction.MARK_DIALOG_READ.value,
                "user_id": 7,
            },
            ErrorCode.CIRCUIT_OPEN,
        ),
        (
            "web",
            WriteAction.SUBMIT_OFFER,
            {
                "action": WriteAction.SUBMIT_OFFER.value,
                "project_id": 77,
                "title": "Offer title",
                "description": "D" * 150,
                "price": 5000,
                "duration_days": 7,
            },
            ErrorCode.TIMEOUT,
        ),
    ],
)
async def test_remote_admission_failure_before_marker_preserves_confirmation(
    config_factory: Callable[..., KworkConfig],
    failure_point: str,
    action: WriteAction,
    request_payload: dict[str, Any],
    error_code: ErrorCode,
) -> None:
    config = config_factory(enable_writes=True, expected_user_id=42)
    store = CoordinationStore(config)
    record, token = await _prepare_custom_record(
        store,
        key=f"admission-{failure_point}",
        action=action,
        request=request_payload,
    )
    gateway = AdmissionFailureGateway(config, store, failure_point)

    with pytest.raises(GatewayError) as failed:
        await gateway.commit_write(
            write_id=record.write_id,
            payload_hash=record.payload_hash,
            confirmation_token=token,
            correlation_id="admission",
        )
    assert failed.value.code is error_code
    persisted = await store.get_write(record.write_id, scope="account-42")
    assert persisted is not None
    assert persisted.state is WriteState.PREPARED
    assert persisted.remote_started_at is None
    assert persisted.lease_owner is None

    retry = CancellationGateway(config, store)
    committed = await retry.commit_write(
        write_id=record.write_id,
        payload_hash=record.payload_hash,
        confirmation_token=token,
        correlation_id="retry",
    )
    assert committed.state is WriteState.SUCCEEDED
    assert retry.remote_calls == 1


@pytest.mark.asyncio
async def test_write_adapter_cannot_report_success_without_crossing_remote_boundary(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory(enable_writes=True, expected_user_id=42)
    store = CoordinationStore(config)
    record, token = await _prepare_record(store, key="missing-remote-boundary")
    gateway = NoBoundaryGateway(config, store)

    with pytest.raises(GatewayError) as caught:
        await gateway.commit_write(
            write_id=record.write_id,
            payload_hash=record.payload_hash,
            confirmation_token=token,
            correlation_id="missing-boundary",
        )
    assert caught.value.code is ErrorCode.CONTRACT_DRIFT
    persisted = await store.get_write(record.write_id, scope="account-42")
    assert persisted is not None
    assert persisted.state is WriteState.PREPARED
    assert persisted.remote_started_at is None
    assert persisted.lease_owner is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        AmbiguousWriteError("before-marker"),
        RuntimeError("before-marker"),
    ],
)
async def test_pre_boundary_adapter_failure_releases_confirmation_for_retry(
    config_factory: Callable[..., KworkConfig],
    failure: BaseException,
) -> None:
    config = config_factory(enable_writes=True, expected_user_id=42)
    store = CoordinationStore(config)
    record, token = await _prepare_record(
        store,
        key=f"immediate-{type(failure).__name__}",
    )
    gateway = ImmediateFailureGateway(config, store, failure)

    with pytest.raises(type(failure)):
        await gateway.commit_write(
            write_id=record.write_id,
            payload_hash=record.payload_hash,
            confirmation_token=token,
            correlation_id="immediate-failure",
        )
    persisted = await store.get_write(record.write_id, scope="account-42")
    assert persisted is not None
    assert persisted.state is WriteState.PREPARED
    assert persisted.remote_started_at is None
    assert persisted.lease_owner is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        AmbiguousWriteError("marker-return-failure"),
        RuntimeError("marker-return-failure"),
    ],
)
async def test_marker_commit_then_callback_failure_is_conservatively_unknown(
    config_factory: Callable[..., KworkConfig],
    failure: BaseException,
) -> None:
    config = config_factory(enable_writes=True, expected_user_id=42)
    store = MarkerCommitThenErrorStore(config, failure)
    record, token = await _prepare_record(
        store,
        key=f"marker-error-{type(failure).__name__}",
    )
    gateway = CancellationGateway(config, store)

    result = await gateway.commit_write(
        write_id=record.write_id,
        payload_hash=record.payload_hash,
        confirmation_token=token,
        correlation_id="marker-error",
    )
    assert result.state is WriteState.SUBMISSION_UNKNOWN
    assert result.reconciliation_required is True
    assert gateway.remote_calls == 0
    persisted = await store.get_write(record.write_id, scope="account-42")
    assert persisted is not None
    assert persisted.remote_started_at is not None
    assert persisted.lease_owner is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage",
    ["identity", "preflight", "exclusive", "before_remote"],
)
async def test_cancellation_before_remote_boundary_restores_prepared_and_retries(
    config_factory: Callable[..., KworkConfig],
    stage: str,
) -> None:
    config = config_factory(enable_writes=True, expected_user_id=42)
    store = CoordinationStore(config)
    record, token = await _prepare_record(store, key=f"cancel-before-{stage}")
    gateway = CancellationGateway(config, store, stage=stage)

    task = _commit_task(gateway, record, token)
    await asyncio.wait_for(gateway.stage_entered().wait(), timeout=2)
    task.cancel()
    await _assert_cancelled(task)

    persisted = await store.get_write(record.write_id, scope="account-42")
    assert persisted is not None
    assert persisted.state is WriteState.PREPARED
    assert persisted.lease_owner is None
    assert persisted.lease_expires is None
    assert persisted.remote_started_at is None
    assert gateway.remote_calls == 0
    async with store.writer_guard("account-42"):
        pass

    retry = CancellationGateway(config, store)
    committed = await retry.commit_write(
        write_id=record.write_id,
        payload_hash=record.payload_hash,
        confirmation_token=token,
        correlation_id="retry",
    )
    assert committed.state is WriteState.SUCCEEDED
    assert retry.remote_calls == 1


@pytest.mark.asyncio
async def test_cancellation_after_remote_call_starts_is_immediately_unknown_and_blocks_account(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory(enable_writes=True, expected_user_id=42)
    store = CoordinationStore(config)
    record, token = await _prepare_record(store, key="cancel-after-remote")
    gateway = CancellationGateway(config, store, stage="remote")

    task = _commit_task(gateway, record, token)
    await asyncio.wait_for(gateway.remote_entered.wait(), timeout=2)
    task.cancel()
    await _assert_cancelled(task)

    persisted = await store.get_write(record.write_id, scope="account-42")
    assert persisted is not None
    assert persisted.state is WriteState.SUBMISSION_UNKNOWN
    assert persisted.remote_started_at is not None
    assert persisted.lease_owner is None
    assert persisted.lease_expires is None
    assert gateway.remote_calls == 1

    replay = await gateway.commit_write(
        write_id=record.write_id,
        payload_hash=record.payload_hash,
        confirmation_token=token,
        correlation_id="replay",
    )
    assert replay.state is WriteState.SUBMISSION_UNKNOWN
    assert gateway.remote_calls == 1

    second, second_token = await _prepare_record(store, key="blocked-by-unknown")
    with pytest.raises(GatewayError) as blocked:
        await gateway.commit_write(
            write_id=second.write_id,
            payload_hash=second.payload_hash,
            confirmation_token=second_token,
            correlation_id="blocked",
        )
    assert blocked.value.code is ErrorCode.AMBIGUOUS_WRITE
    assert blocked.value.reconciliation_required is True
    assert gateway.remote_calls == 1


@pytest.mark.asyncio
async def test_cancellation_after_claim_transaction_waits_then_releases_claim(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory(enable_writes=True, expected_user_id=42)
    store = DelayedClaimStore(config)
    record, token = await _prepare_record(store, key="cancel-after-claim")
    gateway = CancellationGateway(config, store)

    task = _commit_task(gateway, record, token)
    await asyncio.wait_for(store.claim_committed.wait(), timeout=2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    store.claim_return.set()
    await _assert_cancelled(task)

    persisted = await store.get_write(record.write_id, scope="account-42")
    assert persisted is not None
    assert persisted.state is WriteState.PREPARED
    assert persisted.lease_owner is None
    assert persisted.remote_started_at is None
    assert gateway.remote_calls == 0


@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_interrupt_claim_release(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory(enable_writes=True, expected_user_id=42)
    store = DelayedReleaseStore(config)
    record, token = await _prepare_record(store, key="cancel-release-twice")
    gateway = CancellationGateway(config, store, stage="identity")

    task = _commit_task(gateway, record, token)
    await asyncio.wait_for(gateway.test_session.entered.wait(), timeout=2)
    task.cancel()
    await asyncio.wait_for(store.release_entered.wait(), timeout=2)
    task.cancel()
    store.release_allowed.set()
    await _assert_cancelled(task)

    persisted = await store.get_write(record.write_id, scope="account-42")
    assert persisted is not None
    assert persisted.state is WriteState.PREPARED
    assert persisted.lease_owner is None
    assert persisted.remote_started_at is None
    async with store.writer_guard("account-42"):
        pass


@pytest.mark.asyncio
async def test_cancellation_is_reraised_when_pre_remote_settlement_fails_and_stale_claim_recovers(
    config_factory: Callable[..., KworkConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_factory(
        enable_writes=True,
        expected_user_id=42,
        write_lease_seconds=10,
    )
    store = FailingReleaseStore(config)
    record, token = await _prepare_record(store, key="cancel-settlement-failure")
    gateway = CancellationGateway(config, store, stage="identity")

    task = _commit_task(gateway, record, token)
    await asyncio.wait_for(gateway.test_session.entered.wait(), timeout=2)
    task.cancel()
    await _assert_cancelled(task)

    persisted = await store.get_write(record.write_id, scope="account-42")
    assert persisted is not None
    assert persisted.state is WriteState.COMMITTING
    assert persisted.remote_started_at is None
    assert persisted.lease_expires is not None
    monkeypatch.setattr(
        "kwork_mcp.coordination.time.time",
        lambda: persisted.lease_expires + 1,  # type: ignore[operator]
    )
    async with store.writer_guard("account-42"):
        recovered = await store.recover_stale_write(
            write_id=record.write_id,
            scope="account-42",
        )
    assert recovered is not None
    assert recovered.state is WriteState.PREPARED
    assert recovered.lease_owner is None


@pytest.mark.asyncio
async def test_cancellation_during_success_ledger_write_persists_success_before_reraising(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory(enable_writes=True, expected_user_id=42)
    store = DelayedFinishStore(config)
    record, token = await _prepare_record(store, key="cancel-success-ledger")
    gateway = CancellationGateway(config, store)

    task = _commit_task(gateway, record, token)
    await asyncio.wait_for(store.finish_entered.wait(), timeout=2)
    task.cancel()
    store.finish_allowed.set()
    await _assert_cancelled(task)

    persisted = await store.get_write(record.write_id, scope="account-42")
    assert persisted is not None
    assert persisted.state is WriteState.SUCCEEDED
    assert persisted.result_json is not None
    assert persisted.lease_owner is None
    assert gateway.remote_calls == 1


@pytest.mark.asyncio
async def test_cancellation_after_durable_remote_marker_is_unknown_even_before_call_return(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory(enable_writes=True, expected_user_id=42)
    store = DelayedMarkerStore(config)
    record, token = await _prepare_record(store, key="cancel-marker")
    gateway = CancellationGateway(config, store)

    task = _commit_task(gateway, record, token)
    await asyncio.wait_for(store.marker_committed.wait(), timeout=2)
    task.cancel()
    store.marker_return.set()
    await _assert_cancelled(task)

    persisted = await store.get_write(record.write_id, scope="account-42")
    assert persisted is not None
    assert persisted.state is WriteState.SUBMISSION_UNKNOWN
    assert persisted.remote_started_at is not None
    assert persisted.lease_owner is None
    assert gateway.remote_calls == 0
