"""Business-error text classification against real Kwork phrasing."""

from __future__ import annotations

import pytest
from kwork.exceptions import KworkHTTPException

from kwork_mcp.errors import classify_upstream_error
from kwork_mcp.models import ErrorCode


def _rejected(message: str) -> KworkHTTPException:
    return KworkHTTPException(
        "Kwork API rejected /offer",
        status=200,
        endpoint="offer",
        response_json={"success": False, "error": message},
    )


@pytest.mark.parametrize(
    "message",
    [
        "Произошла ошибка, повторите попытку позже",
        "Сервис временно недоступен",
        "Попробуйте позже",
        "Please try again later",
    ],
)
def test_temporary_failures_are_retryable_unavailability(message: str) -> None:
    error = classify_upstream_error(_rejected(message))

    assert error.code is ErrorCode.UPSTREAM_UNAVAILABLE
    assert error.retryable is True
    assert error.safe_to_retry is True


@pytest.mark.parametrize(
    "message",
    [
        "Вы уже отправили предложение по этому проекту",
        "Предложение уже существует",
        "Offer already sent",
    ],
)
def test_real_duplicates_stay_duplicates(message: str) -> None:
    assert classify_upstream_error(_rejected(message)).code is ErrorCode.DUPLICATE


@pytest.mark.parametrize(
    "message",
    [
        "User is already logged in",
        "Повторная авторизация не требуется",
    ],
)
def test_incidental_words_do_not_mean_duplicate(message: str) -> None:
    assert classify_upstream_error(_rejected(message)).code is not ErrorCode.DUPLICATE


@pytest.mark.parametrize(
    "message",
    [
        "Нет доступа к заказу",
        "Доступ запрещён",
        "Access denied",
    ],
)
def test_access_denials_are_permission_errors(message: str) -> None:
    assert classify_upstream_error(_rejected(message)).code is ErrorCode.PERMISSION


def test_unavailable_is_not_a_permission_error() -> None:
    assert classify_upstream_error(_rejected("Раздел недоступен")).code is not ErrorCode.PERMISSION
