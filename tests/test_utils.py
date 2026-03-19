from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from kwork_mcp.utils import (
    check_success,
    extract_error,
    format_date,
    format_timestamp,
    safe_get,
    truncate,
    unwrap_response,
    validate_not_empty,
    validate_one_of,
    validate_page,
    validate_positive_id,
    validate_positive_int,
)

# --- format_timestamp ---


class TestFormatTimestamp:
    def test_none(self) -> None:
        assert format_timestamp(None) == "—"

    def test_zero(self) -> None:
        assert format_timestamp(0) == "—"

    def test_empty_string(self) -> None:
        assert format_timestamp("") == "—"

    def test_unix_int(self) -> None:
        # 2024-01-15 12:00:00 UTC
        assert format_timestamp(1705320000) == "15.01.2024 12:00"

    def test_unix_float(self) -> None:
        assert format_timestamp(1705320000.5) == "15.01.2024 12:00"

    def test_string_passthrough(self) -> None:
        assert format_timestamp("2024-01-15") == "2024-01-15"

    def test_custom_format(self) -> None:
        assert format_timestamp(1705320000, fmt="%Y-%m-%d") == "2024-01-15"


# --- format_date ---


class TestFormatDate:
    def test_none(self) -> None:
        assert format_date(None) == "—"

    def test_unix(self) -> None:
        assert format_date(1705320000) == "15.01.2024"


# --- truncate ---


class TestTruncate:
    def test_none(self) -> None:
        assert truncate(None) == "—"

    def test_empty(self) -> None:
        assert truncate("") == "—"

    def test_short_text(self) -> None:
        assert truncate("hello", 200) == "hello"

    def test_exact_limit(self) -> None:
        text = "a" * 200
        assert truncate(text, 200) == text

    def test_over_limit(self) -> None:
        text = "a" * 250
        result = truncate(text, 200)
        assert result.endswith("...")
        assert len(result) <= 203

    def test_newlines_collapsed(self) -> None:
        assert truncate("line1\nline2\nline3") == "line1 line2 line3"

    def test_custom_limit(self) -> None:
        result = truncate("hello world", 5)
        assert result == "hello..."


# --- safe_get ---


class TestSafeGet:
    def test_first_key(self) -> None:
        assert safe_get({"a": 1, "b": 2}, "a", "b") == 1

    def test_fallback_key(self) -> None:
        assert safe_get({"b": 2}, "a", "b") == 2

    def test_default(self) -> None:
        assert safe_get({}, "a", "b") == "—"

    def test_custom_default(self) -> None:
        assert safe_get({}, "a", default=0) == 0

    def test_none_value_skipped(self) -> None:
        assert safe_get({"a": None, "b": "val"}, "a", "b") == "val"

    def test_empty_string_skipped(self) -> None:
        assert safe_get({"a": "", "b": "val"}, "a", "b") == "val"

    def test_not_a_dict(self) -> None:
        assert safe_get("string", "a") == "—"

    def test_none_input(self) -> None:
        assert safe_get(None, "a") == "—"


# --- unwrap_response ---


class TestUnwrapResponse:
    def test_with_response_key(self) -> None:
        assert unwrap_response({"response": {"data": 1}}) == {"data": 1}

    def test_without_response_key(self) -> None:
        assert unwrap_response({"data": 1}) == {"data": 1}

    def test_none_response(self) -> None:
        assert unwrap_response({"response": None, "other": 1}) == {"response": None, "other": 1}

    def test_empty_list_response(self) -> None:
        assert unwrap_response({"response": []}) == []

    def test_zero_response(self) -> None:
        assert unwrap_response({"response": 0}) == 0

    def test_false_response(self) -> None:
        assert unwrap_response({"response": False}) is False

    def test_non_dict(self) -> None:
        assert unwrap_response([1, 2]) == [1, 2]

    def test_none_input(self) -> None:
        assert unwrap_response(None) is None


# --- check_success ---


class TestCheckSuccess:
    def test_success_true(self) -> None:
        assert check_success({"success": True}) is True

    def test_status_ok(self) -> None:
        assert check_success({"status": "ok"}) is True

    def test_failure(self) -> None:
        assert check_success({"success": False}) is False

    def test_non_dict(self) -> None:
        assert check_success("string") is False

    def test_none(self) -> None:
        assert check_success(None) is False


# --- extract_error ---


class TestExtractError:
    def test_error_key(self) -> None:
        assert extract_error({"error": "bad request"}) == "bad request"

    def test_message_key(self) -> None:
        assert extract_error({"message": "not found"}) == "not found"

    def test_fallback_to_str(self) -> None:
        result = extract_error({"code": 500})
        assert "500" in result

    def test_non_dict(self) -> None:
        assert extract_error("raw error") == "raw error"


# --- validate_positive_int / validate_positive_id ---


class TestValidatePositiveInt:
    def test_valid(self) -> None:
        validate_positive_int(1, "value")  # should not raise

    def test_zero(self) -> None:
        with pytest.raises(ToolError, match="положительным"):
            validate_positive_int(0, "value")

    def test_negative(self) -> None:
        with pytest.raises(ToolError, match="положительным"):
            validate_positive_int(-1, "value")

    def test_alias(self) -> None:
        assert validate_positive_id is validate_positive_int


# --- validate_page ---


class TestValidatePage:
    def test_valid(self) -> None:
        validate_page(1)  # should not raise

    def test_zero(self) -> None:
        with pytest.raises(ToolError, match="страницы"):
            validate_page(0)

    def test_negative(self) -> None:
        with pytest.raises(ToolError, match="страницы"):
            validate_page(-1)


# --- validate_not_empty ---


class TestValidateNotEmpty:
    def test_valid(self) -> None:
        validate_not_empty("hello", "field")  # should not raise

    def test_empty(self) -> None:
        with pytest.raises(ToolError, match="пустым"):
            validate_not_empty("", "field")

    def test_whitespace(self) -> None:
        with pytest.raises(ToolError, match="пустым"):
            validate_not_empty("   ", "field")


# --- validate_one_of ---


class TestValidateOneOf:
    def test_one_provided(self) -> None:
        validate_one_of(user_id=123, username=None)  # should not raise

    def test_both_provided(self) -> None:
        validate_one_of(user_id=123, username="test")  # should not raise

    def test_none_provided(self) -> None:
        with pytest.raises(ToolError, match="Укажите хотя бы один"):
            validate_one_of(user_id=None, username=None)

    def test_all_none(self) -> None:
        with pytest.raises(ToolError, match="Укажите хотя бы один"):
            validate_one_of(a=None, b=None, c=None)
