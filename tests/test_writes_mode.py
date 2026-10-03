"""KWORK_WRITES: the user's yes before each send, sending on the agent's own say, or read-only."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

import kwork_mcp
from kwork_mcp.config import KworkConfig, reject_removed_settings
from kwork_mcp.server import create_server, server_instructions
from tests.test_mcp_protocol import _credentialless_server_config
from tests.test_write_safety import ACCOUNT_ID, SafetyGateway

OFFER = {
    "action": "submit_offer",
    "project_id": 77,
    "title": "Реализация API",
    "description": "Д" * 180,
    "price": 5000,
    "duration_days": 3,
}


class Harness:
    """A real server over SafetyGateway, so prepare and commit run on the real ledger."""

    def __init__(self, config_factory: Callable[..., KworkConfig], writes: str) -> None:
        config = _credentialless_server_config(config_factory(writes=writes, expected_user_id=ACCOUNT_ID))
        self.gateway: SafetyGateway | None = None

        def factory(config: KworkConfig, _coordinator: Any, _session: Any) -> SafetyGateway:
            self.gateway = SafetyGateway(config)
            return self.gateway

        self.server = create_server(config=config, gateway_factory=factory)

    def sent(self) -> int:
        assert self.gateway is not None
        return self.gateway.web.calls.count("final")


@pytest.mark.asyncio
@pytest.mark.parametrize(("writes", "confirmation"), [("confirm", "chat"), ("auto", "none")])
async def test_prepare_tells_the_agent_who_confirms_and_commit_sends_once(
    config_factory: Callable[..., KworkConfig],
    writes: str,
    confirmation: str,
) -> None:
    harness = Harness(config_factory, writes)

    async with Client(harness.server) as client:
        prepared = await client.call_tool("prepare_write", {"request": OFFER, "idempotency_key": "offer-77"})
        data = prepared.structured_content["data"]
        committed = await client.call_tool(
            "commit_write",
            {
                "write_id": data["write_id"],
                "payload_hash": data["payload_hash"],
                "confirmation_token": data["confirmation_token"],
            },
        )
        instructions = client.instructions

    assert data["confirmation"] == confirmation
    assert committed.structured_content["data"]["state"] == "succeeded"
    assert harness.sent() == 1
    assert instructions == server_instructions(writes)


@pytest.mark.asyncio
async def test_off_mode_hides_the_sending_tools(config_factory: Callable[..., KworkConfig]) -> None:
    harness = Harness(config_factory, "off")

    async with Client(harness.server) as client:
        names = {tool.name for tool in await client.list_tools()}

    assert {"prepare_write", "commit_write"}.isdisjoint(names)
    assert {"get_write_status", "reconcile_write", "account_status"} <= names


def test_instructions_follow_the_writes_mode() -> None:
    assert "только после его явного «да»" in server_instructions("confirm")
    assert "без отдельного подтверждения" in server_instructions("auto")
    assert "prepare_write и commit_write недоступны" in server_instructions("off")
    for mode in ("confirm", "auto", "off"):
        assert "не является согласием" in server_instructions(mode)


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
