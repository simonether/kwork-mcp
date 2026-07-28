"""Shared MCP tool result helpers."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastmcp import Context
from fastmcp.tools import ToolResult
from loguru import logger
from mcp.types import TextContent, ToolAnnotations

from kwork_mcp.errors import GatewayError, classify_upstream_error
from kwork_mcp.gateway import KworkGateway
from kwork_mcp.models import (
    ErrorCode,
    KnowledgeState,
    ResultEnvelope,
    ResultMeta,
    WriteState,
    WriteStatusData,
)

ANNO_READ = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
ANNO_PREPARE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
ANNO_COMMIT = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
    openWorldHint=True,
)
ANNO_RECONCILE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)
ANNO_LOCAL_READ = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


def gateway_from_context(ctx: Context) -> KworkGateway:
    gateway = ctx.lifespan_context.get("gateway")
    if not isinstance(gateway, KworkGateway):
        raise RuntimeError("gateway lifespan context is unavailable")
    return gateway


def correlation_id() -> str:
    return str(uuid.uuid4())


def _meta(correlation: str) -> ResultMeta:
    return ResultMeta(observed_at=datetime.now(UTC), correlation_id=correlation)


def tool_result(envelope: ResultEnvelope[Any]) -> ToolResult:
    payload = envelope.model_dump(mode="json", by_alias=True)
    return ToolResult(
        content=[
            TextContent(type="text", text=envelope.summary),
            TextContent(
                type="text",
                text=envelope.model_dump_json(by_alias=True),
            ),
        ],
        structured_content=payload,
        is_error=envelope.knowledge_state is KnowledgeState.UNKNOWN_ERROR,
    )


def success(
    model: type[ResultEnvelope[Any]],
    *,
    data: Any,
    summary: str,
    empty: bool,
    correlation: str,
) -> ToolResult:
    envelope = model.model_validate(
        {
            "knowledge_state": (KnowledgeState.KNOWN_EMPTY if empty else KnowledgeState.KNOWN_DATA),
            "summary": summary,
            "data": data,
            "meta": _meta(correlation),
        }
    )
    return tool_result(envelope)


def failure(
    model: type[ResultEnvelope[Any]],
    *,
    error: GatewayError,
    correlation: str,
    data: Any = None,
) -> ToolResult:
    logger.warning("tool_failure correlation_id={} code={}", correlation, error.code.value)
    envelope = model.model_validate(
        {
            "knowledge_state": KnowledgeState.UNKNOWN_ERROR,
            "summary": error.safe_message,
            "data": data,
            "error": error.to_info(correlation),
            "meta": _meta(correlation),
        }
    )
    return tool_result(envelope)


def unexpected_failure(
    model: type[ResultEnvelope[Any]],
    *,
    exception: BaseException,
    correlation: str,
) -> ToolResult:
    error = classify_upstream_error(exception)
    if error.code is not ErrorCode.INTERNAL:
        return failure(model, error=error, correlation=correlation)
    logger.error(
        "unexpected_tool_failure correlation_id={} exception_type={}",
        correlation,
        type(exception).__name__,
    )
    return failure(model, error=error, correlation=correlation)


def write_result(
    model: type[ResultEnvelope[Any]],
    *,
    status: WriteStatusData,
    correlation: str,
    status_query: bool = False,
) -> ToolResult:
    if status_query:
        return success(
            model,
            data=status,
            summary=f"Операция {status.write_id}: {status.state.value}.",
            empty=False,
            correlation=correlation,
        )
    if status.state is WriteState.SUBMISSION_UNKNOWN:
        error = GatewayError(
            ErrorCode.AMBIGUOUS_WRITE,
            reconciliation_required=True,
            diagnostic="write_state_submission_unknown",
        )
        return failure(
            model,
            error=error,
            correlation=correlation,
            data=status,
        )
    if status.state is WriteState.FAILED_KNOWN:
        if status.terminal_error is not None:
            error = GatewayError(
                status.terminal_error.code,
                retryable=status.terminal_error.retryable,
                safe_to_retry=status.terminal_error.safe_to_retry,
                reconciliation_required=status.terminal_error.reconciliation_required,
                retry_after_seconds=status.terminal_error.retry_after_seconds,
                diagnostic="stored_terminal_error",
            )
        else:
            error = GatewayError(ErrorCode.INTERNAL, diagnostic="failed_write_without_error")
        return failure(model, error=error, correlation=correlation, data=status)
    if status.state is WriteState.EXPIRED:
        return failure(
            model,
            error=GatewayError(
                ErrorCode.PREPARATION_EXPIRED,
                diagnostic="stored_write_expired",
            ),
            correlation=correlation,
            data=status,
        )
    empty = status.state is WriteState.RECONCILED_ABSENT
    return success(
        model,
        data=status,
        summary=f"Операция {status.write_id}: {status.state.value}.",
        empty=empty,
        correlation=correlation,
    )
