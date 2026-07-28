"""Strict MCP input validation that never reflects submitted values."""

from __future__ import annotations

import inspect
from typing import Any, get_type_hints

from fastmcp import FastMCP
from fastmcp.server.dependencies import without_injected_parameters
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult
from fastmcp.tools.function_tool import FunctionTool
from jsonschema import SchemaError
from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema.protocols import Validator
from jsonschema.validators import validator_for
from loguru import logger
from mcp.types import CallToolRequestParams
from pydantic import ConfigDict, TypeAdapter, create_model
from pydantic import ValidationError as PydanticValidationError

from kwork_mcp.errors import GatewayError
from kwork_mcp.models import ErrorCode, ResultEnvelope
from kwork_mcp.tools.common import correlation_id, failure

ValidationOutcome = ResultEnvelope[Any]


class SanitizedStrictInputMiddleware(Middleware):
    """Validate advertised schemas without echoing hostile or secret values.

    MCP SDK 1.28.1's built-in strict validator includes ``ValidationError.message``
    in the response. For ``oneOf`` and scalar constraints that message can contain
    the complete submitted instance. The server disables that low-level validator
    and applies the same JSON Schema validation here, before FastMCP invokes or
    logs function argument validation.
    """

    def __init__(self, server: FastMCP) -> None:
        self._server = server
        self._validators: dict[str, Validator] = {}
        self._semantic_validators: dict[str, TypeAdapter[Any]] = {}

    async def _validator(self, tool_name: str) -> Validator | None:
        cached = self._validators.get(tool_name)
        if cached is not None:
            return cached
        tool = await self._server.get_tool(tool_name)
        if tool is None:
            return None
        schema = tool.to_mcp_tool(name=tool.name).inputSchema
        validator_class = validator_for(schema)
        validator_class.check_schema(schema)
        validator = validator_class(schema)
        self._validators[tool_name] = validator
        return validator

    async def _semantic_validator(self, tool_name: str) -> TypeAdapter[Any] | None:
        cached = self._semantic_validators.get(tool_name)
        if cached is not None:
            return cached
        tool = await self._server.get_tool(tool_name)
        if not isinstance(tool, FunctionTool):
            return None
        wrapper = without_injected_parameters(
            tool.fn,
            run_in_thread=tool.run_in_thread,
        )
        signature = inspect.signature(wrapper)
        hints = get_type_hints(wrapper, include_extras=True)
        fields: dict[str, Any] = {}
        for name, parameter in signature.parameters.items():
            if parameter.kind not in {
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            }:
                raise TypeError("unsupported_tool_parameter_kind")
            annotation = hints.get(name, Any)
            default = ... if parameter.default is inspect.Parameter.empty else parameter.default
            fields[name] = (annotation, default)
        argument_model = create_model(
            "SanitizedToolArguments",
            __config__=ConfigDict(extra="forbid", hide_input_in_errors=True),
            **fields,
        )
        adapter: TypeAdapter[Any] = TypeAdapter(argument_model)
        self._semantic_validators[tool_name] = adapter
        return adapter

    @staticmethod
    def _safe_failure(*, diagnostic: str, internal: bool = False) -> ToolResult:
        correlation = correlation_id()
        return failure(
            ValidationOutcome,
            error=GatewayError(
                ErrorCode.INTERNAL if internal else ErrorCode.VALIDATION,
                diagnostic=diagnostic,
            ),
            correlation=correlation,
        )

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        tool_name = context.message.name
        try:
            validator = await self._validator(tool_name)
        except SchemaError:
            logger.error("mcp_tool_schema_invalid")
            return self._safe_failure(
                diagnostic="tool_input_schema_invalid",
                internal=True,
            )
        except Exception as exc:
            logger.error(
                "mcp_tool_schema_lookup_failed exception_type={}",
                type(exc).__name__,
            )
            return self._safe_failure(
                diagnostic="tool_input_schema_lookup_failed",
                internal=True,
            )
        if validator is None:
            logger.warning("mcp_unknown_tool_delegated")
            return await call_next(context)
        try:
            arguments = context.message.arguments or {}
            validator.validate(arguments)
        except JsonSchemaValidationError:
            logger.warning("mcp_tool_input_validation_failed")
            return self._safe_failure(diagnostic="tool_input_validation_failed")
        except Exception as exc:
            logger.error(
                "mcp_tool_input_validation_failed exception_type={}",
                type(exc).__name__,
            )
            return self._safe_failure(
                diagnostic="tool_input_validation_internal",
                internal=True,
            )
        try:
            semantic_validator = await self._semantic_validator(tool_name)
            if semantic_validator is not None:
                semantic_validator.validate_python(arguments)
        except PydanticValidationError:
            logger.warning("mcp_tool_semantic_validation_failed")
            return self._safe_failure(diagnostic="tool_input_validation_failed")
        except Exception as exc:
            logger.error(
                "mcp_tool_semantic_validation_failed exception_type={}",
                type(exc).__name__,
            )
            return self._safe_failure(
                diagnostic="tool_input_validation_internal",
                internal=True,
            )
        return await call_next(context)
