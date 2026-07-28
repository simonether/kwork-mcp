"""Durable write protocol exposed as ordinary stable MCP tools."""

from __future__ import annotations

from typing import Annotated

from fastmcp import Context, FastMCP
from fastmcp.tools import ToolResult
from pydantic import StringConstraints

from kwork_mcp.errors import GatewayError
from kwork_mcp.models import (
    ErrorCode,
    IdempotencyKey,
    ResultEnvelope,
    WriteRequest,
    WriteStatusData,
)
from kwork_mcp.tools.common import (
    ANNO_COMMIT,
    ANNO_LOCAL_READ,
    ANNO_PREPARE,
    ANNO_RECONCILE,
    correlation_id,
    failure,
    gateway_from_context,
    unexpected_failure,
    write_result,
)

WriteOutcome = ResultEnvelope[WriteStatusData]


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        title="Подготовить безопасную запись Kwork",
        annotations=ANNO_PREPARE,
        output_schema=WriteOutcome.model_json_schema(),
    )
    async def prepare_write(
        request: WriteRequest,
        idempotency_key: IdempotencyKey,
        ctx: Context,
    ) -> ToolResult:
        """Проверить и сохранить точный Kwork write payload без remote side effect.

        Поддерживаемые actions: submit_offer, delete_offer, send_message,
        edit_message, delete_message, mark_dialog_read, submit_order_approval и
        set_kwork_state. Перед подготовкой выполняются свежая account binding check
        и action-specific preflight. Результат содержит payload_hash, TTL и
        HMAC-derived confirmation_token; в ledger хранится только его hash. Exact
        replay того же idempotency_key/request возвращает тот же token, пока запись
        prepared. Другой request с тем же key запрещён.
        """
        correlation = correlation_id()
        try:
            status = await gateway_from_context(ctx).prepare_write(
                request,
                idempotency_key,
                correlation_id=correlation,
            )
            return write_result(WriteOutcome, status=status, correlation=correlation)
        except GatewayError as error:
            return failure(WriteOutcome, error=error, correlation=correlation)
        except Exception as exc:
            return unexpected_failure(WriteOutcome, exception=exc, correlation=correlation)

    @mcp.tool(
        title="Выполнить подготовленную запись Kwork",
        annotations=ANNO_COMMIT,
        output_schema=WriteOutcome.model_json_schema(),
    )
    async def commit_write(
        write_id: Annotated[
            str,
            StringConstraints(
                strip_whitespace=True,
                min_length=36,
                max_length=36,
                pattern=r"^[0-9a-f-]{36}$",
            ),
        ],
        payload_hash: Annotated[
            str,
            StringConstraints(
                strip_whitespace=True,
                min_length=64,
                max_length=64,
                pattern=r"^[0-9a-f]{64}$",
            ),
        ],
        confirmation_token: Annotated[
            str,
            StringConstraints(strip_whitespace=True, min_length=32, max_length=256),
        ],
        ctx: Context,
    ) -> ToolResult:
        """Выполнить ровно подготовленный payload в режиме shared one-writer.

        Commit повторяем только с теми же write_id/payload_hash/token: shared ledger
        вернёт сохранённый результат и не вызовет Kwork повторно. Remote writes никогда
        автоматически не retry. Timeout, proxy loss, 5xx или неподтверждённый offer_id
        дают submission_unknown/isError; после этого commit повторять нельзя — вызовите
        reconcile_write.
        """
        correlation = correlation_id()
        try:
            status = await gateway_from_context(ctx).commit_write(
                write_id=write_id,
                payload_hash=payload_hash,
                confirmation_token=confirmation_token,
                correlation_id=correlation,
            )
            return write_result(WriteOutcome, status=status, correlation=correlation)
        except GatewayError as error:
            return failure(WriteOutcome, error=error, correlation=correlation)
        except Exception as exc:
            return unexpected_failure(WriteOutcome, exception=exc, correlation=correlation)

    @mcp.tool(
        title="Получить состояние записи Kwork",
        annotations=ANNO_LOCAL_READ,
        output_schema=WriteOutcome.model_json_schema(),
    )
    async def get_write_status(
        write_id: Annotated[
            str,
            StringConstraints(
                strip_whitespace=True,
                min_length=36,
                max_length=36,
                pattern=r"^[0-9a-f-]{36}$",
            ),
        ],
        ctx: Context,
    ) -> ToolResult:
        """Прочитать durable state и сохранённый результат без remote write."""
        correlation = correlation_id()
        try:
            status = await gateway_from_context(ctx).get_write_status(
                write_id,
                correlation_id=correlation,
            )
            if status is None:
                return failure(
                    WriteOutcome,
                    error=GatewayError(ErrorCode.NOT_FOUND, diagnostic="write_id_not_found"),
                    correlation=correlation,
                )
            return write_result(
                WriteOutcome,
                status=status,
                correlation=correlation,
                status_query=True,
            )
        except GatewayError as error:
            return failure(WriteOutcome, error=error, correlation=correlation)
        except Exception as exc:
            return unexpected_failure(WriteOutcome, exception=exc, correlation=correlation)

    @mcp.tool(
        title="Сверить неоднозначную запись Kwork",
        annotations=ANNO_RECONCILE,
        output_schema=WriteOutcome.model_json_schema(),
    )
    async def reconcile_write(
        write_id: Annotated[
            str,
            StringConstraints(
                strip_whitespace=True,
                min_length=36,
                max_length=36,
                pattern=r"^[0-9a-f-]{36}$",
            ),
        ],
        ctx: Context,
    ) -> ToolResult:
        """Read back Kwork state for submission_unknown without resubmitting payload.

        Возвращает reconciled_succeeded при найденном точном side effect.
        Reconciled_absent требует нескольких полных отрицательных наблюдений через
        visibility interval; до этого остаётся submission_unknown. Remote write не
        выполняется.
        """
        correlation = correlation_id()
        try:
            status = await gateway_from_context(ctx).reconcile_write(
                write_id,
                correlation_id=correlation,
            )
            if status is None:
                return failure(
                    WriteOutcome,
                    error=GatewayError(ErrorCode.NOT_FOUND, diagnostic="write_id_not_found"),
                    correlation=correlation,
                )
            return write_result(WriteOutcome, status=status, correlation=correlation)
        except GatewayError as error:
            return failure(WriteOutcome, error=error, correlation=correlation)
        except Exception as exc:
            return unexpected_failure(WriteOutcome, exception=exc, correlation=correlation)
