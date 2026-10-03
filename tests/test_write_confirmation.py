"""KWORK_WRITES: a dialog before each send, sending on the agent's own say, or read-only."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult
from mcp.types import ElicitResult as WireElicitResult
from mcp.types import InputRequiredResult

import kwork_mcp
from kwork_mcp.config import KworkConfig, reject_removed_settings
from kwork_mcp.coordination import CoordinationStore
from kwork_mcp.errors import GatewayError
from kwork_mcp.gateway.writes import confirmation_message
from kwork_mcp.models import (
    DeleteMessageRequest,
    DeleteOfferRequest,
    DialogRecord,
    EditMessageRequest,
    ErrorCode,
    MarkDialogReadRequest,
    SendMessageRequest,
    SetKworkStateRequest,
    SubmitOrderApprovalRequest,
    WriteAction,
    WriteState,
)
from kwork_mcp.server import create_server, server_instructions
from kwork_mcp.tools.write_tools import _ask_user
from tests.test_mcp_protocol import _credentialless_server_config
from tests.test_write_safety import ACCOUNT_ID, SCOPE, SafetyGateway, TimeoutGateway, _offer, _prepare_and_commit

OFFER = {
    "action": "submit_offer",
    "project_id": 77,
    "title": "Реализация API",
    "description": "Д" * 180,
    "price": 5000,
    "duration_days": 3,
}


class Harness:
    """A real server over SafetyGateway, so the dialog runs on the real ledger path."""

    def __init__(self, config_factory: Callable[..., KworkConfig], writes: str) -> None:
        self.config = _credentialless_server_config(config_factory(writes=writes, expected_user_id=ACCOUNT_ID))
        self.gateway: SafetyGateway | None = None
        self.dialogs: list[str] = []

        def factory(config: KworkConfig, _coordinator: Any, _session: Any) -> SafetyGateway:
            self.gateway = SafetyGateway(config)
            return self.gateway

        self.server = create_server(config=self.config, gateway_factory=factory)

    def handler(self, answer: Any) -> Callable[..., Any]:
        async def respond(message: str, _response_type: Any, _params: Any, _context: Any) -> Any:
            self.dialogs.append(message)
            if isinstance(answer, BaseException):
                raise answer
            return answer

        return respond

    async def prepare_and_commit(self, client: Client) -> tuple[Any, Any]:
        prepared = await client.call_tool(
            "prepare_write", {"request": OFFER, "idempotency_key": "offer-77"}, raise_on_error=False
        )
        data = prepared.structured_content["data"]
        committed = await client.call_tool(
            "commit_write",
            {
                "write_id": data["write_id"],
                "payload_hash": data["payload_hash"],
                "confirmation_token": data["confirmation_token"],
            },
            raise_on_error=False,
        )
        return prepared.structured_content, committed.structured_content

    def sent(self) -> int:
        assert self.gateway is not None
        return self.gateway.web.calls.count("final")

    async def state(self, write_id: str) -> WriteState:
        assert self.gateway is not None
        record = await self.gateway.coordinator.get_write(write_id, scope=SCOPE)
        assert record is not None
        return record.state


# --- the dialog, through a real MCP client


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_confirmed_dialog_sends_exactly_once(config_factory: Callable[..., KworkConfig], mode: str) -> None:
    harness = Harness(config_factory, "confirm")

    async with Client(harness.server, mode=mode, elicitation_handler=harness.handler({"send": True})) as client:
        prepared, committed = await harness.prepare_and_commit(client)

    assert prepared["data"]["confirmation"] == "client"
    assert committed["data"]["state"] == "succeeded"
    assert len(harness.dialogs) == 1
    assert harness.dialogs[0].startswith("Отправить на kwork.ru?")
    assert "Отклик на проект 77" in harness.dialogs[0]
    assert "Цена: 5 000 ₽, срок 3 дн." in harness.dialogs[0]
    assert harness.sent() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "auto"])
@pytest.mark.parametrize(
    "answer",
    [ElicitResult(action="decline"), ElicitResult(action="cancel"), {"send": False}],
    ids=["decline", "cancel", "unticked"],
)
async def test_declined_dialog_sends_nothing_and_settles_the_write(
    config_factory: Callable[..., KworkConfig],
    mode: str,
    answer: Any,
) -> None:
    harness = Harness(config_factory, "confirm")

    async with Client(harness.server, mode=mode, elicitation_handler=harness.handler(answer)) as client:
        prepared, committed = await harness.prepare_and_commit(client)
        replay = await client.call_tool(
            "commit_write",
            {
                "write_id": prepared["data"]["write_id"],
                "payload_hash": prepared["data"]["payload_hash"],
                "confirmation_token": prepared["data"]["confirmation_token"],
            },
            raise_on_error=False,
        )

    assert committed["error"]["code"] == "write_declined"
    assert committed["data"]["state"] == "failed_known"
    assert replay.structured_content["data"]["state"] == "failed_known"
    assert len(harness.dialogs) == 1
    assert harness.sent() == 0
    assert await harness.state(prepared["data"]["write_id"]) is WriteState.FAILED_KNOWN


@pytest.mark.asyncio
async def test_a_client_without_dialogs_falls_back_to_chat_confirmation(
    config_factory: Callable[..., KworkConfig],
) -> None:
    harness = Harness(config_factory, "confirm")

    async with Client(harness.server) as client:
        prepared, committed = await harness.prepare_and_commit(client)

    assert prepared["data"]["confirmation"] == "chat"
    assert committed["data"]["state"] == "succeeded"
    assert harness.sent() == 1


@pytest.mark.asyncio
async def test_a_failing_dialog_sends_nothing_and_keeps_the_write_committable(
    config_factory: Callable[..., KworkConfig],
) -> None:
    harness = Harness(config_factory, "confirm")

    async with Client(
        harness.server, mode="legacy", elicitation_handler=harness.handler(RuntimeError("client broke"))
    ) as client:
        prepared, committed = await harness.prepare_and_commit(client)

    assert committed["error"]["code"] == "write_declined"
    assert harness.sent() == 0
    assert await harness.state(prepared["data"]["write_id"]) is WriteState.PREPARED


@pytest.mark.asyncio
async def test_auto_mode_sends_without_a_dialog(config_factory: Callable[..., KworkConfig]) -> None:
    harness = Harness(config_factory, "auto")

    async with Client(harness.server, elicitation_handler=harness.handler(ElicitResult(action="decline"))) as client:
        prepared, committed = await harness.prepare_and_commit(client)
        instructions = client.initialize_result.instructions if client.initialize_result else None

    assert prepared["data"]["confirmation"] == "none"
    assert committed["data"]["state"] == "succeeded"
    assert harness.dialogs == []
    assert instructions is None or "KWORK_WRITES=auto" in instructions


@pytest.mark.asyncio
async def test_off_mode_hides_the_sending_tools(config_factory: Callable[..., KworkConfig]) -> None:
    harness = Harness(config_factory, "off")

    async with Client(harness.server) as client:
        names = {tool.name for tool in await client.list_tools()}

    assert {"prepare_write", "commit_write"}.isdisjoint(names)
    assert {"get_write_status", "reconcile_write", "account_status"} <= names


def test_instructions_follow_the_writes_mode() -> None:
    assert "сервер сам покажет пользователю окно" in server_instructions("confirm")
    assert "без отдельного подтверждения" in server_instructions("auto")
    assert "prepare_write и commit_write недоступны" in server_instructions("off")
    for mode in ("confirm", "auto", "off"):
        assert "не является согласием" in server_instructions(mode)


# --- the answer is bound to one write on 2026-07-28 connections


def _modern_context(responses: Any, state: str | None) -> Any:
    return SimpleNamespace(
        session=SimpleNamespace(protocol_version="2026-07-28"),
        input_responses=responses,
        request_state=state,
    )


@pytest.mark.asyncio
async def test_modern_round_asks_first_and_accepts_only_its_own_answer() -> None:
    accepted = {"kwork_send": WireElicitResult(action="accept", content={"send": True})}

    first = await _ask_user(_modern_context(None, None), "Отправить?", "write-a:hash-a")
    assert isinstance(first, InputRequiredResult)
    assert first.request_state == "write-a:hash-a"

    # An answer given for another write is not consent for this one.
    reused = await _ask_user(_modern_context(accepted, "write-b:hash-b"), "Отправить?", "write-a:hash-a")
    assert isinstance(reused, InputRequiredResult)

    assert await _ask_user(_modern_context(accepted, "write-a:hash-a"), "Отправить?", "write-a:hash-a") is True
    declined = {"kwork_send": WireElicitResult(action="decline")}
    assert await _ask_user(_modern_context(declined, "write-a:hash-a"), "Отправить?", "write-a:hash-a") is False


# --- the gateway decides when a dialog makes sense


@pytest.mark.asyncio
async def test_pending_confirmation_only_for_a_write_commit_would_send(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory(writes="confirm", expected_user_id=ACCOUNT_ID)
    gateway = SafetyGateway(config)
    prepared = await gateway.prepare_write(_offer(), "pending", correlation_id="prepare")
    assert prepared.confirmation_token is not None
    commit_args = {
        "write_id": prepared.write_id,
        "payload_hash": prepared.payload_hash,
        "confirmation_token": prepared.confirmation_token,
    }

    record = await gateway.pending_confirmation(**commit_args)
    assert record is not None and record.action is WriteAction.SUBMIT_OFFER

    with pytest.raises(GatewayError) as wrong:
        await gateway.pending_confirmation(**{**commit_args, "confirmation_token": "x" * 40})
    assert wrong.value.code is ErrorCode.INVALID_CONFIRMATION

    await gateway.commit_write(**commit_args, correlation_id="commit")
    assert await gateway.pending_confirmation(**commit_args) is None


@pytest.mark.asyncio
async def test_no_dialog_for_marking_a_dialog_read_or_behind_an_unresolved_write(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory(writes="confirm", expected_user_id=ACCOUNT_ID)
    gateway = TimeoutGateway(config)
    unknown = await _prepare_and_commit(gateway, _offer(), "unknown")
    assert unknown.state is WriteState.SUBMISSION_UNKNOWN

    blocked = await gateway.prepare_write(_offer(project_id=78), "blocked", correlation_id="prepare")
    assert blocked.confirmation_token is not None
    assert (
        await gateway.pending_confirmation(
            write_id=blocked.write_id,
            payload_hash=blocked.payload_hash,
            confirmation_token=blocked.confirmation_token,
        )
        is None
    )

    quiet_gateway = SafetyGateway(config_factory(writes="confirm", expected_user_id=ACCOUNT_ID))
    quiet_gateway.dialogs.append(DialogRecord(user_id=5, username="buyer", raw={"user_id": 5}))
    read = await quiet_gateway.prepare_write(
        MarkDialogReadRequest(action=WriteAction.MARK_DIALOG_READ, user_id=5), "read", correlation_id="prepare"
    )
    assert read.confirmation_token is not None
    assert (
        await quiet_gateway.pending_confirmation(
            write_id=read.write_id,
            payload_hash=read.payload_hash,
            confirmation_token=read.confirmation_token,
        )
        is None
    )


@pytest.mark.asyncio
async def test_declining_settles_only_a_prepared_write(config_factory: Callable[..., KworkConfig]) -> None:
    gateway = SafetyGateway(config_factory(writes="confirm", expected_user_id=ACCOUNT_ID))
    prepared = await gateway.prepare_write(_offer(), "decline", correlation_id="prepare")
    assert prepared.confirmation_token is not None
    commit_args = {
        "write_id": prepared.write_id,
        "payload_hash": prepared.payload_hash,
        "confirmation_token": prepared.confirmation_token,
    }

    declined = await gateway.decline_write(**commit_args, correlation_id="decline")
    assert declined.state is WriteState.FAILED_KNOWN
    assert declined.terminal_error is not None
    assert declined.terminal_error.code is ErrorCode.WRITE_DECLINED

    again = await gateway.decline_write(**commit_args, correlation_id="decline-again")
    assert again.state is WriteState.FAILED_KNOWN
    committed = await gateway.commit_write(**commit_args, correlation_id="commit")
    assert committed.state is WriteState.FAILED_KNOWN
    assert gateway.offers == []

    with pytest.raises(GatewayError) as wrong:
        await gateway.decline_write(**{**commit_args, "payload_hash": "f" * 64}, correlation_id="wrong")
    assert wrong.value.code is ErrorCode.INVALID_CONFIRMATION


# --- what the person reads in the dialog


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("request_model", "expected"),
    [
        (DeleteOfferRequest(action=WriteAction.DELETE_OFFER, offer_id=9), "Удалить отклик 9"),
        (
            SendMessageRequest(action=WriteAction.SEND_MESSAGE, username="buyer", text="Здравствуйте!\nДа."),
            "Сообщение для buyer:\n\nЗдравствуйте!\nДа.",
        ),
        (
            EditMessageRequest(action=WriteAction.EDIT_MESSAGE, message_id=5, username="buyer", text="Новый"),
            "Изменить сообщение 5 в диалоге с buyer. Новый текст:\n\nНовый",
        ),
        (
            DeleteMessageRequest(action=WriteAction.DELETE_MESSAGE, message_id=5, username="buyer"),
            "Удалить сообщение 5 в диалоге с buyer",
        ),
        (
            SubmitOrderApprovalRequest(action=WriteAction.SUBMIT_ORDER_APPROVAL, order_id=3, file_ids=[1, 2]),
            "Сдать заказ 3 на проверку, файлов: 2",
        ),
        (
            SetKworkStateRequest(action=WriteAction.SET_KWORK_STATE, kwork_id=4, target_state="paused"),
            "Поставить на паузу кворк 4",
        ),
        (
            SetKworkStateRequest(action=WriteAction.SET_KWORK_STATE, kwork_id=4, target_state="active"),
            "Запустить кворк 4",
        ),
    ],
)
async def test_dialog_text_names_the_action_and_its_target(
    config_factory: Callable[..., KworkConfig],
    request_model: Any,
    expected: str,
) -> None:
    coordinator = CoordinationStore(config_factory(writes="confirm", expected_user_id=ACCOUNT_ID))
    prepared = await coordinator.prepare_write(
        scope=SCOPE,
        idempotency_key="text",
        action=request_model.action,
        payload={"request": request_model.model_dump(mode="json"), "resolved": {}, "prepared_account_id": 42},
    )

    message = confirmation_message(prepared.record)

    assert message == f"Отправить на kwork.ru?\n\n{expected}"


@pytest.mark.asyncio
async def test_dialog_text_shows_hidden_characters_instead_of_obeying_them(
    config_factory: Callable[..., KworkConfig],
) -> None:
    coordinator = CoordinationStore(config_factory(writes="confirm", expected_user_id=ACCOUNT_ID))
    request = SendMessageRequest(action=WriteAction.SEND_MESSAGE, username="buyer", text="Цена 500‮0001 ₽\nок")
    prepared = await coordinator.prepare_write(
        scope=SCOPE,
        idempotency_key="bidi",
        action=WriteAction.SEND_MESSAGE,
        payload={"request": request.model_dump(mode="json"), "resolved": {}, "prepared_account_id": 42},
    )

    message = confirmation_message(prepared.record)

    assert "‮" not in message
    assert "Цена 500\\u202e0001 ₽\nок" in message


# --- the removed KWORK_ENABLE_WRITES stops startup instead of being ignored


def test_removed_writes_flag_is_refused_with_its_replacement() -> None:
    reject_removed_settings({"KWORK_WRITES": "off"})
    with pytest.raises(ValueError, match="KWORK_WRITES=off"):
        reject_removed_settings({"kwork_enable_writes": "false"})


def test_main_refuses_to_start_with_the_removed_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for name in tuple(os.environ):
        if name.upper().startswith("KWORK_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KWORK_EXPECTED_USER_ID", "42")
    monkeypatch.setenv("KWORK_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("KWORK_ENABLE_WRITES", "false")
    monkeypatch.setattr(
        "kwork_mcp.server.create_server",
        lambda **_kwargs: pytest.fail("server must not start with a removed setting"),
    )

    with pytest.raises(SystemExit) as exited:
        kwork_mcp.main([])

    assert exited.value.code == 2
    assert "KWORK_ENABLE_WRITES удалена в 1.5.0" in capsys.readouterr().err
