from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from io import StringIO
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.tools import ToolResult
from fastmcp.tools.function_tool import FunctionTool
from jsonschema import validate as validate_json
from jsonschema.protocols import Validator
from loguru import logger
from mcp.types import CallToolRequestParams, TextContent

from kwork_mcp.config import KworkConfig
from kwork_mcp.coordination import CoordinationStore
from kwork_mcp.gateway import KworkGateway
from kwork_mcp.middleware import SanitizedStrictInputMiddleware
from kwork_mcp.models import WriteStatusData
from kwork_mcp.server import create_server
from kwork_mcp.session import KworkSessionManager
from kwork_mcp.tools import write_tools


class _StaticTool:
    def __init__(self, name: str, schema: dict[str, Any]) -> None:
        self.name = name
        self._schema = schema

    def to_mcp_tool(self, *, name: str) -> SimpleNamespace:
        assert name == self.name
        return SimpleNamespace(inputSchema=self._schema)


class _FakeServer:
    def __init__(
        self,
        *,
        tools: dict[str, Any] | None = None,
        lookup_error: Exception | None = None,
    ) -> None:
        self.tools = tools or {}
        self.lookup_error = lookup_error
        self.lookups: list[str] = []

    async def get_tool(self, name: str) -> Any | None:
        self.lookups.append(name)
        if self.lookup_error is not None:
            raise self.lookup_error
        return self.tools.get(name)


class _RaisingValidator:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.instances: list[object] = []

    def validate(self, instance: object) -> None:
        self.instances.append(instance)
        raise self.error


class _RecordingValidator:
    def __init__(self) -> None:
        self.instances: list[object] = []

    def validate(self, instance: object) -> None:
        self.instances.append(instance)


class _ForbiddenWriteGateway(KworkGateway):
    def __init__(
        self,
        config: KworkConfig,
        coordinator: CoordinationStore,
        session: KworkSessionManager,
    ) -> None:
        super().__init__(config, coordinator, session)
        self.prepare_calls = 0

    async def prepare_write(
        self,
        request: Any,
        idempotency_key: str,
        *,
        correlation_id: str,
    ) -> WriteStatusData:
        self.prepare_calls += 1
        raise AssertionError("gateway must not receive semantically invalid input")


def _defaulted_tool(value: int = 1) -> str:
    return str(value)


def _variadic_tool(*values: str) -> str:
    return ",".join(values)


@pytest.fixture
def middleware_logs() -> Iterator[StringIO]:
    output = StringIO()
    sink_id = logger.add(output, format="{message}")
    try:
        yield output
    finally:
        logger.remove(sink_id)


def _middleware(server: _FakeServer) -> SanitizedStrictInputMiddleware:
    return SanitizedStrictInputMiddleware(cast(FastMCP, server))


def _credentialless_server_config(config: KworkConfig) -> KworkConfig:
    return config.model_copy(
        update={
            "token": None,
            "expected_user_id": config.expected_user_id or 42,
            "persist_token": True,
        }
    )


def _context(
    name: str,
    arguments: dict[str, Any] | None = None,
) -> MiddlewareContext[CallToolRequestParams]:
    return MiddlewareContext(
        message=CallToolRequestParams(name=name, arguments=arguments),
        method="tools/call",
    )


def _assert_failure(result: ToolResult, code: str) -> None:
    assert result.is_error is True
    assert isinstance(result.structured_content, dict)
    assert result.structured_content["knowledge_state"] == "unknown_error"
    assert result.structured_content["error"]["code"] == code


@pytest.mark.asyncio
async def test_validator_is_built_once_and_reused_from_cache() -> None:
    tool = _StaticTool(
        "bounded",
        {
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
    )
    server = _FakeServer(tools={tool.name: tool})
    middleware = _middleware(server)

    first = await middleware._validator(tool.name)
    second = await middleware._validator(tool.name)

    assert first is second
    assert first is not None
    first.validate({"value": 1})
    assert server.lookups == [tool.name]


@pytest.mark.asyncio
async def test_semantic_validator_is_built_once_and_preserves_defaults() -> None:
    tool = FunctionTool.from_function(_defaulted_tool)
    server = _FakeServer(tools={tool.name: tool})
    middleware = _middleware(server)

    first = await middleware._semantic_validator(tool.name)
    second = await middleware._semantic_validator(tool.name)

    assert first is second
    assert first is not None
    validated = first.validate_python({})
    assert validated.value == 1
    assert server.lookups == [tool.name]


@pytest.mark.asyncio
async def test_unknown_tool_is_delegated_for_protocol_level_rejection(
    middleware_logs: StringIO,
) -> None:
    secret = "unknown-tool-with-secret-credential"
    middleware = _middleware(_FakeServer())
    expected = ToolResult(
        content=[TextContent(type="text", text="protocol layer")],
        structured_content={"delegated": True},
    )
    call_next = AsyncMock(return_value=expected)

    result = await middleware.on_call_tool(_context(secret, {"token": secret}), call_next)

    assert result is expected
    call_next.assert_awaited_once()
    assert secret not in middleware_logs.getvalue()
    assert "mcp_unknown_tool_delegated" in middleware_logs.getvalue()


@pytest.mark.asyncio
async def test_invalid_schema_is_internal_and_does_not_reflect_schema_error(
    middleware_logs: StringIO,
) -> None:
    secret = "schema-secret-must-not-leak"
    tool = _StaticTool(
        "invalid_schema",
        {
            "type": f"invalid-{secret}",
        },
    )
    middleware = _middleware(_FakeServer(tools={tool.name: tool}))
    call_next = AsyncMock()

    result = await middleware.on_call_tool(_context(tool.name), call_next)

    _assert_failure(result, "internal")
    call_next.assert_not_awaited()
    assert secret not in str(result)
    assert secret not in middleware_logs.getvalue()
    assert "mcp_tool_schema_invalid" in middleware_logs.getvalue()


@pytest.mark.asyncio
async def test_lookup_failure_is_internal_and_does_not_reflect_exception(
    middleware_logs: StringIO,
) -> None:
    secret = "lookup-secret-must-not-leak"
    middleware = _middleware(_FakeServer(lookup_error=RuntimeError(secret)))
    call_next = AsyncMock()

    result = await middleware.on_call_tool(_context("lookup_failure"), call_next)

    _assert_failure(result, "internal")
    call_next.assert_not_awaited()
    assert secret not in str(result)
    assert secret not in middleware_logs.getvalue()
    assert "exception_type=RuntimeError" in middleware_logs.getvalue()


@pytest.mark.asyncio
async def test_jsonschema_failure_does_not_reflect_invalid_instance(
    middleware_logs: StringIO,
) -> None:
    secret = "invalid-instance-secret-must-not-leak"
    tool = _StaticTool(
        "strict_value",
        {
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
    )
    middleware = _middleware(_FakeServer(tools={tool.name: tool}))
    call_next = AsyncMock()

    result = await middleware.on_call_tool(
        _context(tool.name, {"value": secret}),
        call_next,
    )

    _assert_failure(result, "validation")
    call_next.assert_not_awaited()
    assert secret not in str(result)
    assert secret not in middleware_logs.getvalue()
    assert "mcp_tool_input_validation_failed" in middleware_logs.getvalue()


@pytest.mark.asyncio
async def test_unexpected_validator_failure_is_internal_and_sanitized(
    middleware_logs: StringIO,
) -> None:
    secret = "validator-secret-must-not-leak"
    validator = _RaisingValidator(RuntimeError(secret))
    middleware = _middleware(_FakeServer())
    middleware._validators["unexpected"] = cast(Validator, validator)
    call_next = AsyncMock()
    arguments = {"password": secret}

    result = await middleware.on_call_tool(
        _context("unexpected", arguments),
        call_next,
    )

    _assert_failure(result, "internal")
    call_next.assert_not_awaited()
    assert validator.instances == [arguments]
    assert secret not in str(result)
    assert secret not in middleware_logs.getvalue()
    assert "exception_type=RuntimeError" in middleware_logs.getvalue()


@pytest.mark.asyncio
async def test_unsupported_tool_signature_is_internal_and_sanitized(
    middleware_logs: StringIO,
) -> None:
    secret = "variadic-secret-must-not-leak"
    tool = FunctionTool(
        name="variadic",
        parameters={"type": "object"},
        fn=_variadic_tool,
    )
    middleware = _middleware(_FakeServer(tools={tool.name: tool}))
    call_next = AsyncMock()

    result = await middleware.on_call_tool(
        _context(tool.name, {"value": secret}),
        call_next,
    )

    _assert_failure(result, "internal")
    call_next.assert_not_awaited()
    assert secret not in str(result)
    assert secret not in middleware_logs.getvalue()
    assert "exception_type=TypeError" in middleware_logs.getvalue()


@pytest.mark.asyncio
async def test_valid_input_delegates_once_and_returns_exact_result() -> None:
    validator = _RecordingValidator()
    middleware = _middleware(_FakeServer())
    middleware._validators["valid"] = cast(Validator, validator)
    arguments = {"value": 7}
    context = _context("valid", arguments)
    expected = ToolResult(
        content=[TextContent(type="text", text="ok")],
        structured_content={"ok": True},
    )
    call_next = AsyncMock(return_value=expected)

    result = await middleware.on_call_tool(context, call_next)

    assert result is expected
    assert validator.instances == [arguments]
    call_next.assert_awaited_once_with(context)


@pytest.mark.asyncio
async def test_semantic_write_validation_is_sanitized_before_tool_and_gateway(
    config_factory: Callable[..., KworkConfig],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "semantic-message-secret-must-never-be-reflected"
    body_marker = Mock(return_value="body-must-not-run")
    monkeypatch.setattr(write_tools, "correlation_id", body_marker)
    gateways: list[_ForbiddenWriteGateway] = []

    def gateway_factory(
        config: KworkConfig,
        coordinator: CoordinationStore,
        session: KworkSessionManager,
    ) -> KworkGateway:
        gateway = _ForbiddenWriteGateway(config, coordinator, session)
        gateways.append(gateway)
        return gateway

    server = create_server(
        config=_credentialless_server_config(config_factory(enable_writes=True, expected_user_id=42)),
        gateway_factory=gateway_factory,
    )
    log_output = StringIO()
    async with Client(server) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        sink_id = logger.add(log_output, format="{message}")
        try:
            result = await client.call_tool(
                "prepare_write",
                {
                    "request": {
                        "action": "send_message",
                        "user_id": 7,
                        "username": "recipient",
                        "text": secret,
                    },
                    "idempotency_key": "semantic-regression",
                },
                raise_on_error=False,
            )
        finally:
            logger.remove(sink_id)

    assert result.is_error is True
    assert isinstance(result.structured_content, dict)
    assert result.structured_content["knowledge_state"] == "unknown_error"
    assert result.structured_content["error"]["code"] == "validation"
    assert tools["prepare_write"].outputSchema is not None
    validate_json(
        instance=result.structured_content,
        schema=tools["prepare_write"].outputSchema,
    )
    assert len(result.content) == 2
    assert json.loads(result.content[1].text) == result.structured_content  # type: ignore[union-attr]
    assert gateways and gateways[0].prepare_calls == 0
    body_marker.assert_not_called()

    stderr = capsys.readouterr().err
    assert secret not in str(result)
    assert secret not in stderr
    assert secret not in caplog.text
    assert secret not in log_output.getvalue()
