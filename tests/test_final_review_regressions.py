from __future__ import annotations

import time
import unicodedata
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from kwork.schema.actor import Actor

from kwork_mcp.config import KworkConfig
from kwork_mcp.coordination import CoordinationStore
from kwork_mcp.errors import AmbiguousWriteError, ContractDriftError, GatewayError
from kwork_mcp.gateway import KworkGateway
from kwork_mcp.models import (
    ConnectsData,
    ErrorCode,
    OfferRecord,
    OrderRecord,
    ProjectRecord,
    SubmitOfferRequest,
    SubmitOrderApprovalRequest,
    WriteAction,
    WriteState,
    WriteStatusData,
)
from kwork_mcp.upstream import GatewayKworkClient


class RedirectResponse:
    def __init__(self, *, url: str, status: int, location: str) -> None:
        self.url = url
        self.status = status
        self.headers = {
            "Content-Type": "text/plain",
            "Location": location,
        }
        self.release_calls = 0

    async def text(self, *, errors: str) -> str:
        assert errors == "replace"
        return ""

    def release(self) -> None:
        self.release_calls += 1


class RedirectResponseContext:
    def __init__(self, response: RedirectResponse) -> None:
        self.response = response

    async def __aenter__(self) -> RedirectResponse:
        return self.response

    async def __aexit__(self, *_args: Any) -> None:
        return None


class RedirectTrapSession:
    def __init__(self, response: RedirectResponse) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def request(self, **kwargs: Any) -> RedirectResponseContext:
        self.calls.append(kwargs)
        if len(self.calls) > 1:
            pytest.fail("POST redirect crossed an origin with its headers or body")
        return RedirectResponseContext(self.response)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [307, 308])
async def test_post_redirect_never_forwards_headers_or_body_off_origin(status: int) -> None:
    client = GatewayKworkClient("", "")
    response = RedirectResponse(
        url="https://kwork.ru/api/offer/createoffer",
        status=status,
        location="https://api.kwork.ru/redirect-collector",
    )
    session = RedirectTrapSession(response)
    client._session = session  # type: ignore[assignment]
    secret_headers = {
        "Authorization": "Bearer must-stay-on-origin",
        "X-CSRF-Token": "csrf-must-stay-on-origin",
    }
    request_body = {"description": "body-must-stay-on-origin"}
    try:
        with pytest.raises(ContractDriftError):
            await client.web.request(
                "POST",
                "api/offer/createoffer",
                data=request_body,
                headers=secret_headers,
                allow_redirects=True,
            )
    finally:
        client._session = None

    assert len(session.calls) == 1
    assert session.calls[0]["url"] == "https://kwork.ru/api/offer/createoffer"
    assert session.calls[0]["headers"] == secret_headers
    assert session.calls[0]["data"] is request_body
    assert response.release_calls == 1


class WriteSession:
    scope = "account-42"

    @asynccontextmanager
    async def exclusive_client(self) -> Any:
        yield

    async def verify_account_identity(self) -> Actor:
        return Actor(id=42, username="fixture")

    async def verify_write_identity(self) -> Actor:
        return Actor(id=42, username="fixture")


class OfferWriteGateway(KworkGateway):
    def __init__(
        self,
        config: KworkConfig,
        coordinator: CoordinationStore,
        *,
        remote_succeeds: bool,
    ) -> None:
        super().__init__(config, coordinator, WriteSession())  # type: ignore[arg-type]
        self.remote_succeeds = remote_succeeds
        self.offers: list[OfferRecord] = []
        self.write_calls = 0

    async def get_project(self, project_id: int) -> ProjectRecord:
        return ProjectRecord(
            project_id=project_id,
            title="Project",
            status="active",
            raw={"id": project_id, "status": "active"},
        )

    async def get_connects(self) -> ConnectsData:
        return ConnectsData(active=5, total=10, raw={"active": 5, "total": 10})

    async def _all_offers(self, max_pages: int = 50) -> list[OfferRecord]:
        return list(self.offers)

    async def get_offer(self, offer_id: int) -> OfferRecord | None:
        return next((offer for offer in self.offers if offer.offer_id == offer_id), None)

    async def _execute_write(
        self,
        _record: Any,
        *,
        before_remote_attempt: Callable[[], Awaitable[None]],
    ) -> dict[str, Any]:
        await before_remote_attempt()
        self.write_calls += 1
        if not self.remote_succeeds:
            raise GatewayError(
                ErrorCode.TIMEOUT,
                retryable=True,
                safe_to_retry=True,
                diagnostic="response_lost_after_submission",
            )
        return {"offer_id": 9001, "project_id": 77}


class ApprovalWriteGateway(KworkGateway):
    def __init__(self, config: KworkConfig, coordinator: CoordinationStore) -> None:
        super().__init__(config, coordinator, WriteSession())  # type: ignore[arg-type]
        self.orders = [OrderRecord(order_id=81, status=1, raw={"id": 81})]

    async def _all_orders(self, max_pages: int = 50) -> list[OrderRecord]:
        return list(self.orders)

    async def _execute_write(
        self,
        _record: Any,
        *,
        before_remote_attempt: Callable[[], Awaitable[None]],
    ) -> dict[str, Any]:
        await before_remote_attempt()
        raise GatewayError(
            ErrorCode.TIMEOUT,
            retryable=True,
            safe_to_retry=True,
            diagnostic="response_lost_after_submission",
        )


class FailConfirmedSuccessOnceStore(CoordinationStore):
    def __init__(self, config: KworkConfig) -> None:
        super().__init__(config)
        self.failed_confirmed_success = False

    async def finish_write(self, **kwargs: Any) -> Any:
        if kwargs["state"] is WriteState.SUCCEEDED and not self.failed_confirmed_success:
            self.failed_confirmed_success = True
            raise GatewayError(ErrorCode.INTERNAL, diagnostic="injected_ledger_failure")
        return await super().finish_write(**kwargs)


def offer_request(
    *,
    title: str = "Реализация API",
    description: str = "Д" * 180,
) -> SubmitOfferRequest:
    return SubmitOfferRequest(
        action=WriteAction.SUBMIT_OFFER,
        project_id=77,
        title=title,
        description=description,
        price=5000,
        duration_days=7,
    )


async def prepare_and_commit_unknown(
    gateway: OfferWriteGateway,
    request: SubmitOfferRequest,
    *,
    idempotency_key: str,
) -> WriteStatusData:
    prepared = await gateway.prepare_write(
        request,
        idempotency_key,
        correlation_id="prepare",
    )
    assert prepared.confirmation_token is not None
    committed = await gateway.commit_write(
        write_id=prepared.write_id,
        payload_hash=prepared.payload_hash,
        confirmation_token=prepared.confirmation_token,
        correlation_id="commit",
    )
    return committed


@pytest.mark.asyncio
async def test_confirmed_remote_success_with_ledger_failure_becomes_typed_unknown(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory(enable_writes=True, expected_user_id=42)
    coordinator = FailConfirmedSuccessOnceStore(config)
    gateway = OfferWriteGateway(config, coordinator, remote_succeeds=True)

    status = await prepare_and_commit_unknown(
        gateway,
        offer_request(),
        idempotency_key="ledger-failure-after-success",
    )

    assert isinstance(status, WriteStatusData)
    assert status.state is WriteState.SUBMISSION_UNKNOWN
    assert status.reconciliation_required is True
    assert status.terminal_error is not None
    assert status.terminal_error.code is ErrorCode.AMBIGUOUS_WRITE
    assert status.terminal_error.reconciliation_required is True
    assert gateway.write_calls == 1
    persisted = await gateway.get_write_status(status.write_id, correlation_id="status")
    assert persisted is not None
    assert persisted.state is WriteState.SUBMISSION_UNKNOWN


@pytest.mark.asyncio
async def test_reconcile_offer_accepts_benign_upstream_text_normalization(
    config_factory: Callable[..., KworkConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_factory(enable_writes=True, expected_user_id=42)
    gateway = OfferWriteGateway(config, CoordinationStore(config), remote_succeeds=False)
    request = offer_request(
        title="  Реализация Cafe\u0301 API  ",
        description=("  Cafe\u0301 и строка\n" + "Д" * 180 + "  "),
    )
    unknown = await prepare_and_commit_unknown(
        gateway,
        request,
        idempotency_key="normalized-offer-readback",
    )
    assert unknown.state is WriteState.SUBMISSION_UNKNOWN
    gateway.offers = [
        OfferRecord(
            offer_id=7001,
            project_id=request.project_id,
            title=f"  {unicodedata.normalize('NFC', request.title)}  ",
            description=(
                f"  {unicodedata.normalize('NFC', request.description).replace(chr(10), chr(13) + chr(10))}  "
            ),
            price=request.price,
            duration_days=request.duration_days,
            created_at=int(time.time()),
            raw={"id": 7001, "project_id": request.project_id},
        )
    ]

    now = time.time()
    monkeypatch.setattr("kwork_mcp.coordination.time.time", lambda: now + 1.1)
    reconciled = await gateway.reconcile_write(
        unknown.write_id,
        correlation_id="reconcile",
    )

    assert reconciled is not None
    assert reconciled.state is WriteState.RECONCILED_SUCCEEDED
    assert reconciled.result == {
        "offer_id": 7001,
        "project_id": 77,
        "reconciled": True,
    }


@pytest.mark.asyncio
async def test_reconcile_other_offer_in_project_stays_unknown_without_absent_observation(
    config_factory: Callable[..., KworkConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_factory(
        enable_writes=True,
        expected_user_id=42,
        reconciliation_absence_interval_seconds=1.0,
    )
    gateway = OfferWriteGateway(config, CoordinationStore(config), remote_succeeds=False)
    request = offer_request()
    unknown = await prepare_and_commit_unknown(
        gateway,
        request,
        idempotency_key="different-offer-candidate",
    )
    assert unknown.state is WriteState.SUBMISSION_UNKNOWN
    gateway.offers = [
        OfferRecord(
            offer_id=7002,
            project_id=request.project_id,
            title="Совсем другое предложение",
            description="И" * 180,
            price=request.price + 1,
            duration_days=request.duration_days + 1,
            created_at=int(time.time()),
            raw={"id": 7002, "project_id": request.project_id},
        )
    ]

    with pytest.raises(AmbiguousWriteError):
        await gateway.reconcile_write(unknown.write_id, correlation_id="candidate")
    persisted = await gateway.get_write_status(unknown.write_id, correlation_id="status")
    assert persisted is not None
    assert persisted.state is WriteState.SUBMISSION_UNKNOWN

    gateway.offers = []
    now = time.time()
    monkeypatch.setattr("kwork_mcp.coordination.time.time", lambda: now + 1.1)
    first_real_absence = await gateway.reconcile_write(
        unknown.write_id,
        correlation_id="first-real-absence",
    )
    assert first_real_absence is not None
    assert first_real_absence.state is WriteState.SUBMISSION_UNKNOWN


@pytest.mark.asyncio
async def test_unknown_order_status_cannot_be_negative_reconciliation_evidence(
    config_factory: Callable[..., KworkConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_factory(enable_writes=True, expected_user_id=42)
    gateway = ApprovalWriteGateway(config, CoordinationStore(config))
    prepared = await gateway.prepare_write(
        SubmitOrderApprovalRequest(
            action=WriteAction.SUBMIT_ORDER_APPROVAL,
            order_id=81,
        ),
        "approval-status-transition",
        correlation_id="prepare",
    )
    assert prepared.confirmation_token
    unknown = await gateway.commit_write(
        write_id=prepared.write_id,
        payload_hash=prepared.payload_hash,
        confirmation_token=prepared.confirmation_token,
        correlation_id="commit",
    )
    assert unknown.state is WriteState.SUBMISSION_UNKNOWN

    gateway.orders = [OrderRecord(order_id=81, status=3, raw={"id": 81})]
    now = time.time()
    monkeypatch.setattr("kwork_mcp.coordination.time.time", lambda: now + 1.1)
    with pytest.raises(AmbiguousWriteError):
        await gateway.reconcile_write(prepared.write_id, correlation_id="reconcile")

    persisted = await gateway.get_write_status(prepared.write_id, correlation_id="status")
    assert persisted is not None
    assert persisted.state is WriteState.SUBMISSION_UNKNOWN
