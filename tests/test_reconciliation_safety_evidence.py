from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, cast

import pytest

from kwork_mcp.config import KworkConfig
from kwork_mcp.coordination import CoordinationStore, StoredWrite
from kwork_mcp.errors import AmbiguousWriteError
from kwork_mcp.gateway import KworkGateway
from kwork_mcp.models import (
    DialogRecord,
    ItemCollection,
    KworkRecord,
    MessageRecord,
    OrderRecord,
    PageInfo,
    SubmitOrderApprovalRequest,
    WriteAction,
    WriteState,
)
from kwork_mcp.session import KworkSessionManager


class _UnusedSession:
    """The exercised private read-back paths only use fake gateway reads."""


class ReconciliationFake(KworkGateway):
    def __init__(self, config: KworkConfig) -> None:
        super().__init__(
            config,
            CoordinationStore(config),
            cast(KworkSessionManager, _UnusedSession()),
        )
        self.dialogs: list[DialogRecord] = []
        self.messages: list[MessageRecord] = []
        self.orders: list[OrderRecord] = []
        self.kworks: list[KworkRecord] = []

    async def list_dialogs(self, page: int = 1) -> ItemCollection[DialogRecord]:
        return ItemCollection[DialogRecord](
            items=self.dialogs,
            page=PageInfo(page=page, has_more=False),
        )

    async def get_dialog(
        self,
        username: str,
        page: int = 1,
    ) -> ItemCollection[MessageRecord]:
        return ItemCollection[MessageRecord](
            items=self.messages,
            page=PageInfo(page=page, has_more=False),
        )

    async def _all_orders(self, max_pages: int = 50) -> list[OrderRecord]:
        return self.orders

    async def list_my_kworks(self) -> ItemCollection[KworkRecord]:
        return ItemCollection[KworkRecord](items=self.kworks)


def stored_unknown(action: WriteAction) -> StoredWrite:
    now = time.time()
    return StoredWrite(
        write_id=f"unknown-{action.value}",
        scope="account-42",
        idempotency_key=f"key-{action.value}",
        action=action,
        payload_json="{}",
        payload_hash="a" * 64,
        state=WriteState.SUBMISSION_UNKNOWN,
        prepared_at=now - 1,
        expires_at=now + 60,
        updated_at=now,
        lease_owner=None,
        lease_expires=None,
        remote_started_at=None,
        result_json=None,
        error_json=None,
    )


@pytest.mark.asyncio
async def test_mark_dialog_read_unknown_unread_count_is_ambiguous(
    config_factory: Callable[..., KworkConfig],
) -> None:
    gateway = ReconciliationFake(config_factory(expected_user_id=42))
    gateway.dialogs = [
        DialogRecord(
            user_id=71,
            username="recipient",
            unread_count=None,
            raw={"user_id": 71},
        )
    ]

    with pytest.raises(AmbiguousWriteError):
        await gateway._read_back(
            WriteAction.MARK_DIALOG_READ,
            {"user_id": 71},
            {},
            stored_unknown(WriteAction.MARK_DIALOG_READ),
        )


@pytest.mark.asyncio
async def test_order_approval_accepts_numeric_string_statuses(
    config_factory: Callable[..., KworkConfig],
) -> None:
    gateway = ReconciliationFake(config_factory(expected_user_id=42))
    request = SubmitOrderApprovalRequest(
        action=WriteAction.SUBMIT_ORDER_APPROVAL,
        order_id=81,
    )
    gateway.orders = [OrderRecord(order_id=81, status="1", raw={"id": 81})]

    resolved: dict[str, Any] = {}
    await gateway._preflight(request, resolved)
    assert resolved["order_status_at_prepare"] == 1

    gateway.orders = [OrderRecord(order_id=81, status="4", raw={"id": 81})]
    succeeded, result = await gateway._read_back(
        WriteAction.SUBMIT_ORDER_APPROVAL,
        {"order_id": 81},
        resolved,
        stored_unknown(WriteAction.SUBMIT_ORDER_APPROVAL),
    )

    assert succeeded is True
    assert result["submitted_for_approval"] is True


@pytest.mark.asyncio
async def test_set_kwork_state_unknown_status_is_ambiguous(
    config_factory: Callable[..., KworkConfig],
) -> None:
    gateway = ReconciliationFake(config_factory(expected_user_id=42))
    gateway.kworks = [
        KworkRecord(
            kwork_id=91,
            status_group_name=None,
            raw={"id": 91},
        )
    ]

    with pytest.raises(AmbiguousWriteError):
        await gateway._read_back(
            WriteAction.SET_KWORK_STATE,
            {"kwork_id": 91, "target_state": "active"},
            {},
            stored_unknown(WriteAction.SET_KWORK_STATE),
        )


@pytest.mark.parametrize(
    ("action", "request_data"),
    [
        (
            WriteAction.EDIT_MESSAGE,
            {"username": "recipient", "message_id": 101, "text": "updated"},
        ),
        (
            WriteAction.DELETE_MESSAGE,
            {"username": "recipient", "message_id": 101},
        ),
    ],
)
@pytest.mark.asyncio
async def test_message_mutation_missing_stable_ids_is_not_false_evidence(
    config_factory: Callable[..., KworkConfig],
    action: WriteAction,
    request_data: dict[str, Any],
) -> None:
    gateway = ReconciliationFake(config_factory(expected_user_id=42))
    gateway.messages = [
        MessageRecord(
            message_id=None,
            sender_id=42,
            text="updated",
            created_at=int(time.time()),
            raw={"text": "updated"},
        )
    ]

    with pytest.raises(AmbiguousWriteError):
        await gateway._read_back(
            action,
            request_data,
            {},
            stored_unknown(action),
        )


@pytest.mark.asyncio
async def test_send_message_normalizes_benign_text_but_rejects_real_difference(
    config_factory: Callable[..., KworkConfig],
) -> None:
    gateway = ReconciliationFake(config_factory(expected_user_id=42))
    now = int(time.time())
    request = {
        "username": "recipient",
        "text": "  Cafe\u0301\r\nsecond line  ",
    }
    gateway.messages = [
        MessageRecord(
            message_id=111,
            sender_id=42,
            text="Café\nsecond line",
            created_at=now,
            raw={"id": 111},
        )
    ]

    succeeded, result = await gateway._read_back(
        WriteAction.SEND_MESSAGE,
        request,
        {"user_id": 71},
        stored_unknown(WriteAction.SEND_MESSAGE),
    )
    assert succeeded is True
    assert result["message_id"] == 111

    gateway.messages = [
        MessageRecord(
            message_id=112,
            sender_id=42,
            text="Café\nmaterially different",
            created_at=now,
            raw={"id": 112},
        )
    ]
    with pytest.raises(AmbiguousWriteError):
        await gateway._read_back(
            WriteAction.SEND_MESSAGE,
            request,
            {"user_id": 71},
            stored_unknown(WriteAction.SEND_MESSAGE),
        )
