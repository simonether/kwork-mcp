from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from mcp.types import ToolAnnotations


def format_timestamp(value: Any, fmt: str = "%d.%m.%Y %H:%M") -> str:
    """Format a unix timestamp, date string, or None to a human-readable string."""
    if not value:
        return "—"
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC).strftime(fmt)
    return str(value)


def format_date(ts: Any) -> str:
    """Format a unix timestamp to date-only string (dd.mm.yyyy)."""
    return format_timestamp(ts, fmt="%d.%m.%Y")


def truncate(text: str | None, limit: int = 200) -> str:
    """Truncate text to *limit* characters, collapsing newlines."""
    if not text:
        return "—"
    text = text.replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def safe_get(data: Any, *keys: str, default: Any = "—") -> Any:
    """Try each key in order on *data* (dict), return first truthy value or *default*."""
    if not isinstance(data, dict):
        return default
    for key in keys:
        val = data.get(key)
        if val is not None and val != "":
            return val
    return default


def unwrap_response(data: Any) -> Any:
    """Unwrap ``{"response": ...}`` envelope, returning the inner value or *data* as-is."""
    if isinstance(data, dict):
        resp = data.get("response")
        return resp if resp is not None else data
    return data


def check_success(data: Any) -> bool:
    """Return True if the API response indicates success."""
    if not isinstance(data, dict):
        return False
    return bool(data.get("success") or data.get("status") == "ok")


def extract_error(data: Any) -> str:
    """Extract an error message from the API response."""
    if isinstance(data, dict):
        return str(data.get("error") or data.get("message") or data)
    return str(data)


def validate_positive_int(value: int, name: str) -> None:
    """Raise ToolError if *value* is not a positive integer."""
    if not isinstance(value, int) or value <= 0:
        from fastmcp.exceptions import ToolError

        raise ToolError(f"{name} должен быть положительным целым числом, получено: {value}")


# Alias for backwards compatibility and semantic clarity in ID contexts
validate_positive_id = validate_positive_int


def validate_page(value: int) -> None:
    """Raise ToolError if *value* is less than 1."""
    if not isinstance(value, int) or value < 1:
        from fastmcp.exceptions import ToolError

        raise ToolError(f"Номер страницы должен быть >= 1, получено: {value}")


ANNO_READ = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True)
ANNO_WRITE = ToolAnnotations(openWorldHint=True)
ANNO_DESTRUCTIVE = ToolAnnotations(destructiveHint=True, openWorldHint=True)


def validate_not_empty(value: str, name: str) -> None:
    """Raise ToolError if *value* is empty after stripping."""
    if not isinstance(value, str) or not value.strip():
        from fastmcp.exceptions import ToolError

        raise ToolError(f"{name} не может быть пустым.")


def validate_one_of(**kwargs: Any) -> None:
    """Raise ToolError if none of the keyword arguments has a non-None value."""
    provided = [k for k, v in kwargs.items() if v is not None]
    if not provided:
        from fastmcp.exceptions import ToolError

        names = ", ".join(kwargs.keys())
        raise ToolError(f"Укажите хотя бы один из параметров: {names}")
