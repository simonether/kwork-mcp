"""Typed, sanitized gateway errors.

Raw upstream messages may contain remote content or request details.  They are
used only for classification and are never copied into MCP results or logs.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from typing import Any

import aiohttp
from aiohttp_socks import (
    ProxyConnectionError as SocksProxyConnectionError,
)
from aiohttp_socks import (
    ProxyError as SocksProxyError,
)
from aiohttp_socks import (
    ProxyTimeoutError as SocksProxyTimeoutError,
)
from aiohttp_socks import (
    SocksConnectionError,
    SocksError,
)
from kwork.exceptions import KworkException, KworkHTTPException, KworkRetryExceeded
from pydantic import ValidationError

from kwork_mcp.models import ErrorCode, ErrorInfo

_MAX_RETRY_HINT_SECONDS = 900.0

_SAFE_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.AUTH_REQUIRED: "Авторизация Kwork не настроена.",
    ErrorCode.AUTH_EXPIRED: "Сессия Kwork недействительна; требуется новая авторизация.",
    ErrorCode.AUTH_IN_PROGRESS: "Авторизация Kwork уже выполняется другим процессом.",
    ErrorCode.ACCOUNT_BINDING_REQUIRED: "Для операций записи требуется ожидаемый Kwork user ID.",
    ErrorCode.ACCOUNT_MISMATCH: "Авторизованный Kwork-аккаунт не совпадает с ожидаемым.",
    ErrorCode.CAPTCHA: "Kwork требует пройти капчу в браузере и обновить авторизацию.",
    ErrorCode.PERMISSION: "Kwork отклонил операцию из-за прав доступа.",
    ErrorCode.IP_BLOCKED: "Kwork отклонил запрос для текущего IP-адреса.",
    ErrorCode.CSRF: "Kwork отклонил web-запрос из-за CSRF/session validation.",
    ErrorCode.RATE_LIMIT: "Достигнут лимит запросов Kwork.",
    ErrorCode.CIRCUIT_OPEN: "Запросы к этому маршруту временно остановлены после серии сбоев.",
    ErrorCode.PROXY: "Не удалось подключиться к Kwork через настроенный прокси.",
    ErrorCode.TIMEOUT: "Истекло время ожидания ответа Kwork.",
    ErrorCode.CLOSED_PROJECT: "Проект закрыт или больше не принимает предложения.",
    ErrorCode.DUPLICATE: "Такая операция уже выполнена или предложение уже существует.",
    ErrorCode.INSUFFICIENT_CONNECTS: "Недостаточно коннектов для отправки предложения.",
    ErrorCode.CONTRACT_DRIFT: "Ответ или API-контракт Kwork не соответствует закреплённой версии.",
    ErrorCode.CREDENTIAL_UPDATE_UNKNOWN: (
        "Не удалось подтвердить долговечность обновления credential store; "
        "проверьте авторизацию перед повторным bootstrap."
    ),
    ErrorCode.AMBIGUOUS_WRITE: (
        "Результат записи неизвестен. Не повторяйте commit; сначала выполните reconcile_write."
    ),
    ErrorCode.IDEMPOTENCY_CONFLICT: "Idempotency key уже связан с другим payload.",
    ErrorCode.PREPARATION_EXPIRED: "Подготовленная операция истекла и не может быть выполнена.",
    ErrorCode.INVALID_CONFIRMATION: "Подтверждение не соответствует подготовленной операции.",
    ErrorCode.WRITE_IN_PROGRESS: "Эту операцию уже выполняет другой процесс.",
    ErrorCode.WRITE_DISABLED: "Операции записи отключены конфигурацией.",
    ErrorCode.NOT_FOUND: "Запрошенный объект Kwork не найден.",
    ErrorCode.VALIDATION: "Параметры операции не прошли проверку.",
    ErrorCode.UPSTREAM_UNAVAILABLE: "Kwork временно недоступен.",
    ErrorCode.INTERNAL: "Внутренняя ошибка шлюза.",
}


@dataclass(slots=True)
class GatewayError(Exception):
    code: ErrorCode
    retryable: bool = False
    safe_to_retry: bool = False
    reconciliation_required: bool = False
    retry_after_seconds: float | None = None
    diagnostic: str | None = None

    def __post_init__(self) -> None:
        if self.retry_after_seconds is not None:
            retry_after = float(self.retry_after_seconds)
            self.retry_after_seconds = (
                min(retry_after, _MAX_RETRY_HINT_SECONDS) if math.isfinite(retry_after) and retry_after >= 0 else None
            )
        Exception.__init__(self, _SAFE_MESSAGES[self.code])

    @property
    def safe_message(self) -> str:
        return _SAFE_MESSAGES[self.code]

    def to_info(self, correlation_id: str) -> ErrorInfo:
        return ErrorInfo(
            code=self.code,
            message=self.safe_message,
            retryable=self.retryable,
            safe_to_retry=self.safe_to_retry,
            reconciliation_required=self.reconciliation_required,
            retry_after_seconds=self.retry_after_seconds,
            correlation_id=correlation_id,
        )


class ContractDriftError(GatewayError):
    def __init__(self, diagnostic: str) -> None:
        super().__init__(ErrorCode.CONTRACT_DRIFT, diagnostic=diagnostic)


class AmbiguousWriteError(GatewayError):
    def __init__(self, diagnostic: str | None = None) -> None:
        super().__init__(
            ErrorCode.AMBIGUOUS_WRITE,
            retryable=False,
            safe_to_retry=False,
            reconciliation_required=True,
            diagnostic=diagnostic,
        )


def _payload_text(payload: dict[str, Any] | None, fallback: str) -> str:
    if not payload:
        return fallback.lower()
    values = [
        payload.get("error"),
        payload.get("message"),
        payload.get("response"),
    ]
    return " ".join(str(value) for value in values if value is not None).lower()


def _business_error(text: str) -> ErrorCode | None:
    patterns: tuple[tuple[tuple[str, ...], ErrorCode], ...] = (
        (
            (
                "invalid token",
                "wrong token",
                "token expired",
                "expired token",
                "неверный токен",
                "сессия истек",
            ),
            ErrorCode.AUTH_EXPIRED,
        ),
        (
            (
                "authorization required",
                "authentication required",
                "требуется авторизац",
            ),
            ErrorCode.AUTH_REQUIRED,
        ),
        (("captcha", "капч"), ErrorCode.CAPTCHA),
        (("csrf", "csrftoken", "xsrf"), ErrorCode.CSRF),
        (
            ("too many requests", "rate limit", "слишком много запрос"),
            ErrorCode.RATE_LIMIT,
        ),
        (
            (
                "insufficient connect",
                "not enough connect",
                "недостаточно коннект",
                "не хватает коннект",
            ),
            ErrorCode.INSUFFICIENT_CONNECTS,
        ),
        (("closed", "закрыт", "не принимает"), ErrorCode.CLOSED_PROJECT),
        (
            (
                "already sent",
                "already exists",
                "already",
                "offer exists",
                "duplicate",
                "уже отправ",
                "уже существует",
                "повтор",
            ),
            ErrorCode.DUPLICATE,
        ),
        (
            (
                "ip blocked",
                "ip address",
                "ip-address",
                "ip-адрес",
                "вашего ip",
                "айпи",
            ),
            ErrorCode.IP_BLOCKED,
        ),
        (("permission", "forbidden", "доступ"), ErrorCode.PERMISSION),
        (("not found", "не найден"), ErrorCode.NOT_FOUND),
    )
    for needles, code in patterns:
        if any(needle in text for needle in needles):
            return code
    return None


def _classified_business_error(
    code: ErrorCode,
    *,
    diagnostic: str,
) -> GatewayError:
    if code is ErrorCode.RATE_LIMIT:
        return GatewayError(
            code,
            retryable=True,
            safe_to_retry=True,
            diagnostic=diagnostic,
        )
    return GatewayError(code, diagnostic=diagnostic)


def classify_upstream_error(exc: BaseException) -> GatewayError:
    """Map an upstream/transport exception to a stable safe taxonomy."""

    if isinstance(exc, GatewayError):
        return exc

    if isinstance(exc, KworkHTTPException):
        payload = exc.response_json
        code_value = payload.get("error_code") if isinstance(payload, dict) else None
        text = _payload_text(payload, str(exc))
        if exc.status is not None and exc.status >= 500:
            return GatewayError(
                ErrorCode.UPSTREAM_UNAVAILABLE,
                retryable=True,
                safe_to_retry=True,
                diagnostic=f"http_status={exc.status}",
            )
        if exc.status == 429:
            retry_after = None
            if isinstance(payload, dict):
                raw_retry_after = payload.get("retry_after", payload.get("retryAfter"))
                if isinstance(raw_retry_after, int | float) and not isinstance(raw_retry_after, bool):
                    numeric_retry_after = float(raw_retry_after)
                    if math.isfinite(numeric_retry_after) and numeric_retry_after >= 0:
                        retry_after = min(numeric_retry_after, _MAX_RETRY_HINT_SECONDS)
            return GatewayError(
                ErrorCode.RATE_LIMIT,
                retryable=True,
                safe_to_retry=True,
                retry_after_seconds=retry_after,
                diagnostic="http_status=429",
            )
        if exc.status == 408:
            return GatewayError(
                ErrorCode.TIMEOUT,
                retryable=True,
                safe_to_retry=True,
                diagnostic="http_status=408",
            )
        if exc.status == 401:
            return GatewayError(ErrorCode.AUTH_EXPIRED, diagnostic="http_status=401")
        if str(code_value) == "118":
            return GatewayError(ErrorCode.CAPTCHA, diagnostic="kwork_error_code=118")
        business = _business_error(text)
        if business is not None:
            return _classified_business_error(
                business,
                diagnostic=f"http_status={exc.status}",
            )
        if exc.status == 403:
            return GatewayError(ErrorCode.PERMISSION, diagnostic="http_status=403")
        if exc.status == 404:
            return GatewayError(ErrorCode.NOT_FOUND, diagnostic="http_status=404")
        if exc.status == 409:
            return GatewayError(ErrorCode.DUPLICATE, diagnostic="http_status=409")
        if exc.status is not None and 400 <= exc.status < 500:
            return GatewayError(ErrorCode.PERMISSION, diagnostic=f"http_status={exc.status}")
        return GatewayError(
            ErrorCode.CONTRACT_DRIFT,
            diagnostic=f"invalid_http_response_status={exc.status}",
        )

    if isinstance(exc, KworkRetryExceeded):
        return classify_upstream_error(exc.last_error or RuntimeError("retry exhausted"))

    # The pinned typed pykwork helpers deserialize successful envelopes directly
    # with dictionary indexing and Pydantic models. These exceptions describe an
    # upstream response-shape drift, not an internal gateway fault.
    if isinstance(exc, (KeyError, ValidationError)):
        return GatewayError(
            ErrorCode.CONTRACT_DRIFT,
            diagnostic=f"upstream_parser={type(exc).__name__}",
        )

    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return GatewayError(
            ErrorCode.TIMEOUT,
            retryable=True,
            safe_to_retry=True,
            diagnostic=type(exc).__name__,
        )

    if isinstance(
        exc,
        (
            aiohttp.ClientProxyConnectionError,
            aiohttp.ClientHttpProxyError,
            SocksProxyConnectionError,
            SocksProxyError,
            SocksProxyTimeoutError,
            SocksConnectionError,
            SocksError,
        ),
    ):
        return GatewayError(
            ErrorCode.PROXY,
            retryable=True,
            safe_to_retry=True,
            diagnostic=type(exc).__name__,
        )

    if isinstance(exc, (aiohttp.ClientConnectionError, aiohttp.ServerDisconnectedError, OSError)):
        return GatewayError(
            ErrorCode.UPSTREAM_UNAVAILABLE,
            retryable=True,
            safe_to_retry=True,
            diagnostic=type(exc).__name__,
        )

    if isinstance(exc, KworkException):
        text = str(exc).lower()
        business = _business_error(text)
        if business is not None:
            return _classified_business_error(
                business,
                diagnostic=type(exc).__name__,
            )
        return GatewayError(ErrorCode.UPSTREAM_UNAVAILABLE, diagnostic=type(exc).__name__)

    return GatewayError(ErrorCode.INTERNAL, diagnostic=type(exc).__name__)


def is_auth_error(exc: BaseException) -> bool:
    return classify_upstream_error(exc).code in {
        ErrorCode.AUTH_REQUIRED,
        ErrorCode.AUTH_EXPIRED,
    }


def is_transient_error(exc: BaseException) -> bool:
    return classify_upstream_error(exc).code in {
        ErrorCode.RATE_LIMIT,
        ErrorCode.AUTH_IN_PROGRESS,
        ErrorCode.PROXY,
        ErrorCode.TIMEOUT,
        ErrorCode.UPSTREAM_UNAVAILABLE,
        ErrorCode.CIRCUIT_OPEN,
    }
