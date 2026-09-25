"""Pure helpers for decoding Kwork envelopes, paging and loosely typed scalars."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import unicodedata
from datetime import UTC, datetime
from typing import Any

from kwork.exceptions import KworkHTTPException
from pydantic import JsonValue

from kwork_mcp.errors import ContractDriftError, classify_upstream_error
from kwork_mcp.security import sanitize_external


async def _finish_shielded_task[ResultT](
    task: asyncio.Future[ResultT],
) -> tuple[ResultT, bool]:
    """Finish a ledger task and report cancellation received while shielded."""

    cancelled = False
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError:
            if task.done() and task.cancelled():
                raise
            cancelled = True


def _as_json_dict(
    value: Any,
    route: str,
    *,
    secrets_to_redact: tuple[str, ...] = (),
) -> dict[str, JsonValue]:
    clean = sanitize_external(value, secrets=secrets_to_redact)
    if not isinstance(clean, dict):
        raise ContractDriftError(f"{route}:expected_object")
    return clean


def _as_json_list(
    value: Any,
    route: str,
    *,
    secrets_to_redact: tuple[str, ...] = (),
) -> list[JsonValue]:
    clean = sanitize_external(value, secrets=secrets_to_redact)
    if not isinstance(clean, list):
        raise ContractDriftError(f"{route}:expected_array")
    return clean


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isdigit() and int(value) > 0:
        return int(value)
    return None


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return None


def _optional_number(value: Any) -> int | float | None:
    return value if isinstance(value, int | float) and not isinstance(value, bool) else None


def _optional_scalar(value: Any) -> str | int | None:
    return value if isinstance(value, str | int) and not isinstance(value, bool) else None


def _normalize_remote_text(value: str) -> str:
    return unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n").strip()


def _epoch_seconds(value: int | str | None) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return float(value)
    normalized = value.strip()
    try:
        numeric = float(normalized)
    except ValueError:
        pass
    else:
        return numeric if math.isfinite(numeric) and numeric >= 0 else None
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    timestamp = parsed.timestamp()
    return timestamp if math.isfinite(timestamp) and timestamp >= 0 else None


def _response(
    data: Any,
    route: str,
    *,
    require_response: bool = True,
) -> Any:
    if not isinstance(data, dict):
        raise ContractDriftError(f"{route}:envelope_not_object")
    if data.get("success") is not True:
        error = KworkHTTPException(
            f"Kwork rejected {route}",
            status=200,
            endpoint=route,
            response_json=data,
        )
        raise classify_upstream_error(error)
    if require_response and "response" not in data:
        raise ContractDriftError(f"{route}:missing_response")
    return data.get("response")


def _page_metadata(
    data: dict[str, Any],
    *,
    secrets_to_redact: tuple[str, ...] = (),
) -> dict[str, JsonValue]:
    metadata = {key: value for key, value in data.items() if key != "response"}
    return _as_json_dict(
        metadata,
        "page_metadata",
        secrets_to_redact=secrets_to_redact,
    )


def _strict_paging(
    data: dict[str, Any],
    route: str,
    *,
    requested_page: int,
    item_count: int,
) -> tuple[int, int | None, int | None, int]:
    paging = data.get("paging")
    if not isinstance(paging, dict) or not paging:
        raise ContractDriftError(f"{route}:missing_paging")
    page_value = paging.get("page")
    if isinstance(page_value, bool) or not isinstance(page_value, int) or page_value < 1:
        raise ContractDriftError(f"{route}:paging_page_invalid")
    if page_value != requested_page:
        raise ContractDriftError(f"{route}:paging_page_mismatch")

    def optional_exact_int(name: str, *, minimum: int) -> int | None:
        if name not in paging:
            return None
        value = paging[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ContractDriftError(f"{route}:paging_{name}_invalid")
        return value

    limit = optional_exact_int("limit", minimum=1)
    total = optional_exact_int("total", minimum=0)
    pages = optional_exact_int("pages", minimum=0)
    computed_pages = math.ceil(total / limit) if total is not None and limit is not None else None
    if pages is None:
        pages = computed_pages
    elif computed_pages is not None and pages != computed_pages:
        raise ContractDriftError(f"{route}:paging_pages_inconsistent")
    if pages is None:
        raise ContractDriftError(f"{route}:paging_incomplete")
    if pages == 0:
        if requested_page != 1 or item_count != 0 or total not in {None, 0}:
            raise ContractDriftError(f"{route}:paging_empty_inconsistent")
    elif requested_page > pages:
        raise ContractDriftError(f"{route}:paging_page_out_of_range")
    if limit is not None and item_count > limit:
        raise ContractDriftError(f"{route}:paging_item_count_exceeds_limit")
    if total is not None and item_count > total:
        raise ContractDriftError(f"{route}:paging_item_count_exceeds_total")
    if pages > 0 and requested_page < pages and item_count == 0:
        raise ContractDriftError(f"{route}:paging_premature_empty_page")
    if total is not None and limit is not None and pages > 0:
        expected_count = min(limit, max(0, total - (requested_page - 1) * limit))
        if item_count != expected_count:
            raise ContractDriftError(f"{route}:paging_item_count_inconsistent")
    elif total is not None and total > 0 and item_count == 0:
        raise ContractDriftError(f"{route}:paging_item_count_inconsistent")
    return page_value, limit, total, pages


def _fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
