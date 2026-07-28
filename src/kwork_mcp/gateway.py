"""Typed Kwork API gateway and durable prepare/commit/reconcile orchestration."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import secrets
import string
import time
import unicodedata
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal, cast
from urllib.parse import unquote, urljoin

from kwork import Kwork
from kwork.exceptions import KworkHTTPException
from loguru import logger
from pydantic import JsonValue, TypeAdapter, ValidationError
from yarl import URL

from kwork_mcp.config import KworkConfig
from kwork_mcp.contracts import enforce_route_params
from kwork_mcp.coordination import (
    CoordinationStore,
    CursorCodec,
    StoredWrite,
    pages_from_paging,
)
from kwork_mcp.errors import (
    AmbiguousWriteError,
    ContractDriftError,
    GatewayError,
    classify_upstream_error,
)
from kwork_mcp.models import (
    AccountData,
    CategoryRecord,
    ConnectsData,
    DialogRecord,
    ErrorCode,
    ErrorInfo,
    ItemCollection,
    KworkRecord,
    MessageRecord,
    OfferRecord,
    OrderRecord,
    PageInfo,
    ProjectDiscoveryData,
    ProjectRecord,
    RawObjectData,
    UserRecord,
    WriteAction,
    WriteRequest,
    WriteState,
    WriteStatusData,
)
from kwork_mcp.security import sanitize_external
from kwork_mcp.session import KworkSessionManager

_WRITE_REQUEST_ADAPTER: TypeAdapter[WriteRequest] = TypeAdapter(WriteRequest)


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


class KworkGateway:
    def __init__(
        self,
        config: KworkConfig,
        coordinator: CoordinationStore,
        session: KworkSessionManager,
    ) -> None:
        self.config = config
        self.coordinator = coordinator
        self.session = session
        self.cursor_codec = CursorCodec(coordinator)
        self.instance_id = str(uuid.uuid4())

    @property
    def _redaction_secrets(self) -> tuple[str, ...]:
        runtime = getattr(self.session, "redaction_secrets", None)
        return tuple(runtime) if runtime is not None else self.config.redaction_secrets

    async def account_status(self) -> AccountData:
        actor = await self.session.call_read("account", lambda client: client.get_me())
        if actor.id is None or not actor.username:
            raise ContractDriftError("actor_without_stable_identity")
        return AccountData(
            user_id=actor.id,
            username=actor.username,
            expected_user_id=self.config.expected_user_id,
            expected_username=self.config.expected_username,
            binding_state=("bound" if self.config.expected_user_id is not None else "unbound_reads_only"),
            writes_enabled=self.config.enable_writes,
            write_ready=(
                self.config.enable_writes
                and self.config.expected_user_id is not None
                and actor.id == self.config.expected_user_id
            ),
            raw=_as_json_dict(
                actor,
                "actor",
                secrets_to_redact=self._redaction_secrets,
            ),
        )

    async def get_connects(self) -> ConnectsData:
        connects = await self.session.call_read(
            "connects",
            lambda client: client.get_connects(),
        )
        if connects.active_connects is None or connects.all_connects is None:
            raise ContractDriftError("connects:missing_balance_fields")
        return ConnectsData(
            active=connects.active_connects,
            total=connects.all_connects,
            raw=_as_json_dict(
                connects,
                "connects",
                secrets_to_redact=self._redaction_secrets,
            ),
        )

    async def get_user(
        self,
        *,
        user_id: int | None,
        username: str | None,
    ) -> UserRecord | None:
        if (user_id is None) == (username is None):
            raise GatewayError(
                ErrorCode.VALIDATION,
                diagnostic="provide_exactly_one_user_identifier",
            )
        if user_id is not None:
            try:
                user = await self.session.call_read(
                    "user",
                    lambda client: client.get_user(user_id),
                )
            except GatewayError as error:
                if error.code is ErrorCode.NOT_FOUND:
                    return None
                raise
            if user.id is None:
                raise ContractDriftError("user_without_id")
            return UserRecord(
                user_id=user.id,
                username=user.username,
                raw=_as_json_dict(
                    user,
                    "user",
                    secrets_to_redact=self._redaction_secrets,
                ),
            )

        clean_username = cast(str, username).lstrip("@").strip()
        if not clean_username:
            raise GatewayError(
                ErrorCode.VALIDATION,
                diagnostic="username_empty_after_normalization",
            )
        params = {"username": clean_username}
        enforce_route_params("user_by_username", params)
        data = await self.session.call_read(
            "user-by-username",
            lambda client: client.user_by_username(use_token=True, **params),
        )
        raw_response = _response(data, "userByUsername")
        if raw_response is None or raw_response == []:
            return None
        raw = _as_json_dict(
            raw_response,
            "userByUsername.response",
            secrets_to_redact=self._redaction_secrets,
        )
        resolved_id = _positive_int(raw.get("id"))
        if resolved_id is None:
            raise ContractDriftError("userByUsername:missing_id")
        resolved_username = raw.get("username")
        return UserRecord(
            user_id=resolved_id,
            username=resolved_username if isinstance(resolved_username, str) else None,
            raw=raw,
        )

    async def search_users(self, query: str, page: int) -> ItemCollection[UserRecord]:
        params: dict[str, Any] = {"query": query, "page": page}
        enforce_route_params("user_search", params)
        data = await self.session.call_read(
            "user-search",
            lambda client: client.user_search(use_token=True, **params),
        )
        raw_response = _response(data, "userSearch")
        if isinstance(raw_response, dict):
            if "users" in raw_response:
                raw_items = raw_response["users"]
            elif "items" in raw_response:
                raw_items = raw_response["items"]
            else:
                raise ContractDriftError("userSearch:missing_items")
        else:
            raw_items = raw_response
        if not isinstance(raw_items, list):
            raise ContractDriftError("userSearch:items_not_array")
        items: list[UserRecord] = []
        for item in raw_items:
            raw = _as_json_dict(
                item,
                "userSearch.item",
                secrets_to_redact=self._redaction_secrets,
            )
            user_id = _positive_int(raw.get("id"))
            if user_id is None:
                raise ContractDriftError("userSearch:item_missing_id")
            username = raw.get("username")
            items.append(
                UserRecord(
                    user_id=user_id,
                    username=username if isinstance(username, str) else None,
                    raw=raw,
                )
            )
        paging_raw = data.get("paging") if isinstance(data, dict) else None
        paging = paging_raw if isinstance(paging_raw, dict) else {"page": page}
        current, limit, total, pages = pages_from_paging(paging, len(items))
        return ItemCollection[UserRecord](
            items=items,
            page=PageInfo(
                page=current,
                page_size=limit,
                total_items=total,
                total_pages=pages,
                has_more=current < pages if pages is not None else bool(items),
            ),
            raw_metadata=_page_metadata(
                data,
                secrets_to_redact=self._redaction_secrets,
            ),
        )

    async def discover_projects(
        self,
        *,
        mode: Literal["favorites", "all", "category_ids"],
        category_ids: list[int] | None,
        price_from: int | None,
        price_to: int | None,
        hiring_from: int | None,
        offers_from: int | None,
        offers_to: int | None,
        query: str | None,
        cursor: str | None,
    ) -> ProjectDiscoveryData:
        normalized_categories = sorted(set(category_ids or []))
        if len(normalized_categories) > 100:
            raise GatewayError(
                ErrorCode.VALIDATION,
                diagnostic="too_many_category_ids",
            )
        if mode == "category_ids" and not normalized_categories:
            raise GatewayError(ErrorCode.VALIDATION, diagnostic="category_ids_required")
        if mode != "category_ids" and normalized_categories:
            raise GatewayError(
                ErrorCode.VALIDATION,
                diagnostic="category_ids_only_for_category_mode",
            )
        if price_from is not None and price_to is not None and price_from > price_to:
            raise GatewayError(ErrorCode.VALIDATION, diagnostic="invalid_price_range")
        if offers_from is not None and offers_to is not None and offers_from > offers_to:
            raise GatewayError(ErrorCode.VALIDATION, diagnostic="invalid_offers_range")
        categories = {
            "favorites": "",
            "all": "all",
            "category_ids": ",".join(str(value) for value in normalized_categories),
        }[mode]
        filter_payload = {
            "mode": mode,
            "category_ids": normalized_categories,
            "price_from": price_from,
            "price_to": price_to,
            "hiring_from": hiring_from,
            "offers_from": offers_from,
            "offers_to": offers_to,
            "query": query,
        }
        query_fingerprint = _fingerprint(filter_payload)
        page = 1
        cursor_scope: str | None = None
        if cursor:
            decoded = await self.cursor_codec.decode(cursor)
            if (
                decoded.get("kind") != "projects"
                or decoded.get("fingerprint") != query_fingerprint
                or _positive_int(decoded.get("page")) is None
            ):
                raise GatewayError(ErrorCode.VALIDATION, diagnostic="cursor_filter_mismatch")
            decoded_scope = decoded.get("scope")
            if not isinstance(decoded_scope, str):
                raise GatewayError(ErrorCode.VALIDATION, diagnostic="cursor_filter_mismatch")
            cursor_scope = decoded_scope
            page = cast(int, _positive_int(decoded["page"]))

        params: dict[str, Any] = {
            "categories": categories,
            "price_from": price_from,
            "price_to": price_to,
            "hiring_from": hiring_from,
            "kworks_filter_from": offers_from,
            "kworks_filter_to": offers_to,
            "page": page,
            "query": query,
        }
        params = {key: value for key, value in params.items() if value is not None}
        enforce_route_params("projects", params)
        data, authenticated_scope = await self.session.call_read_scoped(
            "projects",
            lambda client: client.projects(use_token=True, **params),
            expected_scope=cursor_scope,
        )
        raw_response = _response(data, "projects")
        if not isinstance(raw_response, list):
            raise ContractDriftError("projects:response_not_array")
        items = [self._project_from_raw(item) for item in raw_response]
        current, limit, total, pages = _strict_paging(
            data,
            "projects",
            requested_page=page,
            item_count=len(items),
        )
        has_more = current < pages
        next_cursor = None
        if has_more:
            next_cursor = await self.cursor_codec.encode(
                {
                    "v": 1,
                    "kind": "projects",
                    "scope": authenticated_scope,
                    "page": current + 1,
                    "fingerprint": query_fingerprint,
                }
            )
        watermarks = [
            (timestamp, item.project_id)
            for item in items
            if (timestamp := _epoch_seconds(item.published_at)) is not None
        ]
        high_watermark = None
        if watermarks:
            timestamp, project_id = max(watermarks)
            timestamp_text = (
                str(int(timestamp)) if timestamp.is_integer() else f"{timestamp:.6f}".rstrip("0").rstrip(".")
            )
            high_watermark = f"{timestamp_text}:{project_id}"
        connects_raw = data.get("connects")
        connects = (
            _as_json_dict(
                connects_raw,
                "projects.connects",
                secrets_to_redact=self._redaction_secrets,
            )
            if isinstance(connects_raw, dict)
            else None
        )
        return ProjectDiscoveryData(
            mode=mode,
            category_ids=normalized_categories,
            projects=ItemCollection[ProjectRecord](
                items=items,
                page=PageInfo(
                    page=current,
                    page_size=limit,
                    total_items=total,
                    total_pages=pages,
                    has_more=has_more,
                    next_cursor=next_cursor,
                    query_fingerprint=query_fingerprint,
                    high_watermark=high_watermark,
                ),
                raw_metadata=_page_metadata(
                    data,
                    secrets_to_redact=self._redaction_secrets,
                ),
            ),
            connects=connects,
        )

    def _project_from_raw(self, value: Any) -> ProjectRecord:
        raw = _as_json_dict(
            value,
            "project",
            secrets_to_redact=self._redaction_secrets,
        )
        project_id = _positive_int(raw.get("id"))
        if project_id is None:
            raise ContractDriftError("project:missing_id")
        title = raw.get("title")
        description = raw.get("description")
        customer_username = raw.get("username")
        return ProjectRecord(
            project_id=project_id,
            title=title if isinstance(title, str) else None,
            description=description if isinstance(description, str) else None,
            status=_optional_scalar(raw.get("status")),
            customer_id=_positive_int(raw.get("user_id")),
            customer_username=customer_username if isinstance(customer_username, str) else None,
            price=_optional_number(raw.get("price")),
            possible_price_limit=_optional_number(raw.get("possible_price_limit")),
            offers_count=_optional_int(raw.get("offers")),
            category_id=_positive_int(raw.get("category_id")),
            published_at=_optional_scalar(raw.get("date_confirm")),
            raw=raw,
        )

    async def get_project(self, project_id: int) -> ProjectRecord | None:
        params = {"id": project_id}
        enforce_route_params("project", params)
        try:
            data = await self.session.call_read(
                "project",
                lambda client: client.project(use_token=True, **params),
            )
        except GatewayError as error:
            if error.code is ErrorCode.NOT_FOUND:
                return None
            raise
        raw_response = _response(data, "project")
        if raw_response is None or raw_response == []:
            return None
        if isinstance(raw_response, list):
            if not raw_response:
                return None
            if len(raw_response) != 1:
                raise ContractDriftError("project:multiple_records")
            raw_response = raw_response[0]
        return self._project_from_raw(raw_response)

    async def get_exchange_info(self) -> RawObjectData:
        enforce_route_params("exchange_info", {})
        data = await self.session.call_read(
            "exchange-info",
            lambda client: client.exchange_info(use_token=True),
        )
        raw_response = _response(data, "exchangeInfo")
        if not isinstance(raw_response, dict | list):
            raise ContractDriftError("exchangeInfo:unexpected_response")
        raw = sanitize_external(
            raw_response,
            secrets=self._redaction_secrets,
        )
        if not isinstance(raw, dict | list):  # pragma: no cover - sanitizer invariant
            raise ContractDriftError("exchangeInfo:not_json")
        return RawObjectData(raw=raw)

    def _offer_from_raw(self, value: Any) -> OfferRecord | None:
        raw = _as_json_dict(
            value,
            "offer",
            secrets_to_redact=self._redaction_secrets,
        )
        offer_id = _positive_int(raw.get("id"))
        if offer_id is None:
            raise ContractDriftError("offer:missing_id")
        nested_project = raw.get("project")
        nested_project_id = _positive_int(nested_project.get("id")) if isinstance(nested_project, dict) else None
        project_id = _positive_int(raw.get("want_id")) or _positive_int(raw.get("project_id")) or nested_project_id
        if project_id is None:
            return None
        title_value = raw.get("title") or raw.get("name") or raw.get("kwork_name")
        description_value = raw.get("description") or raw.get("comment")
        return OfferRecord(
            offer_id=offer_id,
            project_id=project_id,
            title=title_value if isinstance(title_value, str) else None,
            description=description_value if isinstance(description_value, str) else None,
            status=_optional_scalar(raw.get("status")),
            price=_optional_number(raw.get("price") or raw.get("kwork_price")),
            duration_days=_optional_int(raw.get("duration") or raw.get("kwork_duration")),
            created_at=_optional_scalar(raw.get("date_create") or raw.get("created_at")),
            raw=raw,
        )

    async def get_offer(self, offer_id: int) -> OfferRecord | None:
        params = {"id": offer_id}
        enforce_route_params("offer", params)
        try:
            data = await self.session.call_read(
                "offer",
                lambda client: client.offer(use_token=True, **params),
            )
        except GatewayError as error:
            if error.code is ErrorCode.NOT_FOUND:
                return None
            raise
        raw_response = _response(data, "offer")
        if raw_response is None or raw_response == []:
            return None
        if isinstance(raw_response, list):
            if len(raw_response) != 1:
                raise ContractDriftError("offer:unexpected_record_count")
            raw_response = raw_response[0]
        record = self._offer_from_raw(raw_response)
        if record is None:
            raise ContractDriftError("offer:missing_project_id")
        return record

    async def list_my_offers(self, page: int = 1) -> ItemCollection[OfferRecord]:
        params = {"page": page}
        enforce_route_params("offers", params)
        data = await self.session.call_read(
            "offers",
            lambda client: client.offers(use_token=True, **params),
        )
        raw_response = _response(data, "offers")
        if isinstance(raw_response, dict):
            if "offers" in raw_response:
                raw_items = raw_response["offers"]
            elif "items" in raw_response:
                raw_items = raw_response["items"]
            else:
                raise ContractDriftError("offers:missing_items")
        else:
            raw_items = raw_response
        if not isinstance(raw_items, list):
            raise ContractDriftError("offers:items_not_array")
        items: list[OfferRecord] = []
        for raw_item in raw_items:
            candidate = self._offer_from_raw(raw_item)
            if candidate is None:
                raw = _as_json_dict(
                    raw_item,
                    "offers.item",
                    secrets_to_redact=self._redaction_secrets,
                )
                offer_id = _positive_int(raw.get("id"))
                if offer_id is None:
                    raise ContractDriftError("offers:item_missing_id")
                candidate = await self.get_offer(offer_id)
                if candidate is None:
                    raise ContractDriftError("offers:item_disappeared_during_enrichment")
            items.append(candidate)
        current, limit, total, pages = _strict_paging(
            data,
            "offers",
            requested_page=page,
            item_count=len(items),
        )
        return ItemCollection[OfferRecord](
            items=items,
            page=PageInfo(
                page=current,
                page_size=limit,
                total_items=total,
                total_pages=pages,
                has_more=current < pages,
            ),
            raw_metadata=_page_metadata(
                data,
                secrets_to_redact=self._redaction_secrets,
            ),
        )

    async def list_worker_orders(self, page: int = 1) -> ItemCollection[OrderRecord]:
        params: dict[str, Any] = {"filter": "all", "page": page}
        enforce_route_params("worker_orders", params)
        data = await self.session.call_read(
            "worker-orders",
            lambda client: client.worker_orders(use_token=True, **params),
        )
        raw_response = _response(data, "workerOrders")
        if not isinstance(raw_response, dict):
            raise ContractDriftError("workerOrders:response_not_object")
        raw_items = raw_response.get("orders")
        if not isinstance(raw_items, list):
            raise ContractDriftError("workerOrders:orders_not_array")
        items: list[OrderRecord] = []
        for value in raw_items:
            raw = _as_json_dict(
                value,
                "workerOrders.order",
                secrets_to_redact=self._redaction_secrets,
            )
            order_id = _positive_int(raw.get("id"))
            if order_id is None:
                raise ContractDriftError("workerOrders:order_missing_id")
            payer = raw.get("payer")
            buyer_id = _positive_int(payer.get("id")) if isinstance(payer, dict) else None
            buyer_username_value = payer.get("username") if isinstance(payer, dict) else None
            buyer_username = buyer_username_value if isinstance(buyer_username_value, str) else None
            title = raw.get("display_title") or raw.get("kwork_title") or raw.get("title")
            items.append(
                OrderRecord(
                    order_id=order_id,
                    status=_optional_scalar(raw.get("status")),
                    title=title if isinstance(title, str) else None,
                    buyer_id=buyer_id,
                    buyer_username=buyer_username,
                    raw=raw,
                )
            )
        current, limit, total, pages = _strict_paging(
            {"paging": raw_response.get("paging")},
            "workerOrders",
            requested_page=page,
            item_count=len(items),
        )
        return ItemCollection[OrderRecord](
            items=items,
            page=PageInfo(
                page=current,
                page_size=limit,
                total_items=total,
                total_pages=pages,
                has_more=current < pages,
            ),
            raw_metadata=_as_json_dict(
                {key: value for key, value in raw_response.items() if key != "orders"},
                "workerOrders.metadata",
                secrets_to_redact=self._redaction_secrets,
            ),
        )

    async def get_order_details(self, order_id: int) -> RawObjectData | None:
        params = {"orderId": order_id}
        enforce_route_params("get_order_details", params)
        try:
            data = await self.session.call_read(
                "order-details",
                lambda client: client.get_order_details(use_token=True, **params),
            )
        except GatewayError as error:
            if error.code is ErrorCode.NOT_FOUND:
                return None
            raise
        raw_response = _response(data, "getOrderDetails")
        if raw_response is None or raw_response == []:
            return None
        if not isinstance(raw_response, dict | list):
            raise ContractDriftError("getOrderDetails:unexpected_response")
        raw = sanitize_external(
            raw_response,
            secrets=self._redaction_secrets,
        )
        if not isinstance(raw, dict | list):  # pragma: no cover
            raise ContractDriftError("getOrderDetails:not_json")
        return RawObjectData(raw=raw)

    async def list_dialogs(self, page: int = 1) -> ItemCollection[DialogRecord]:
        params = {"page": page}
        enforce_route_params("dialogs", params)
        data = await self.session.call_read(
            "dialogs",
            lambda client: client.dialogs(use_token=True, **params),
        )
        raw_response = _response(data, "dialogs")
        if not isinstance(raw_response, list):
            raise ContractDriftError("dialogs:response_not_array")
        items: list[DialogRecord] = []
        for value in raw_response:
            raw = _as_json_dict(
                value,
                "dialogs.item",
                secrets_to_redact=self._redaction_secrets,
            )
            username = raw.get("username")
            if not isinstance(username, str) or not username:
                raise ContractDriftError("dialogs:item_missing_username")
            unread_value = raw["unread_count"] if "unread_count" in raw else raw.get("unread")
            items.append(
                DialogRecord(
                    user_id=_positive_int(raw.get("user_id")),
                    username=username,
                    unread_count=_optional_int(unread_value),
                    raw=raw,
                )
            )
        current, limit, total, pages = _strict_paging(
            data,
            "dialogs",
            requested_page=page,
            item_count=len(items),
        )
        return ItemCollection[DialogRecord](
            items=items,
            page=PageInfo(
                page=current,
                page_size=limit,
                total_items=total,
                total_pages=pages,
                has_more=current < pages,
            ),
            raw_metadata=_page_metadata(
                data,
                secrets_to_redact=self._redaction_secrets,
            ),
        )

    async def get_dialog(
        self,
        username: str,
        page: int = 1,
    ) -> ItemCollection[MessageRecord]:
        username = username.lstrip("@").strip()
        if not username:
            raise GatewayError(
                ErrorCode.VALIDATION,
                diagnostic="username_empty_after_normalization",
            )
        params: dict[str, Any] = {"username": username, "page": page}
        enforce_route_params("inboxes", params)
        data = await self.session.call_read(
            "dialog-messages",
            lambda client: client.inboxes(use_token=True, **params),
        )
        raw_response = _response(data, "inboxes")
        if not isinstance(raw_response, list):
            raise ContractDriftError("inboxes:response_not_array")
        items: list[MessageRecord] = []
        for value in raw_response:
            raw = _as_json_dict(
                value,
                "inboxes.item",
                secrets_to_redact=self._redaction_secrets,
            )
            sender_username = raw.get("from_username")
            text = raw.get("message")
            items.append(
                MessageRecord(
                    message_id=_positive_int(raw.get("message_id")),
                    sender_id=_positive_int(raw.get("from_id")),
                    sender_username=sender_username if isinstance(sender_username, str) else None,
                    text=text if isinstance(text, str) else None,
                    created_at=_optional_scalar(raw.get("time")),
                    raw=raw,
                )
            )
        current, limit, total, pages = _strict_paging(
            data,
            "inboxes",
            requested_page=page,
            item_count=len(items),
        )
        return ItemCollection[MessageRecord](
            items=items,
            page=PageInfo(
                page=current,
                page_size=limit,
                total_items=total,
                total_pages=pages,
                has_more=current < pages,
            ),
            raw_metadata=_page_metadata(
                data,
                secrets_to_redact=self._redaction_secrets,
            ),
        )

    async def list_my_kworks(self) -> ItemCollection[KworkRecord]:
        enforce_route_params("kworks_status_list", {})
        data = await self.session.call_read(
            "kworks",
            lambda client: client.kworks_status_list(use_token=True),
        )
        raw_response = _response(data, "kworksStatusList")
        if not isinstance(raw_response, list):
            raise ContractDriftError("kworksStatusList:response_not_array")
        actor = self.session.actor
        actor_id = _positive_int(actor.id) if actor is not None else None
        if actor_id is None:
            raise ContractDriftError("kworksStatusList:authenticated_actor_missing")
        items: list[KworkRecord] = []
        seen_ids: set[int] = set()

        def parse_item(
            item_value: Any,
            *,
            group_id: int,
            group_name: str,
        ) -> KworkRecord:
            item = _as_json_dict(
                item_value,
                "kwork.item",
                secrets_to_redact=self._redaction_secrets,
            )
            kwork_id = _positive_int(item.get("id"))
            if kwork_id is None:
                raise ContractDriftError("kwork.item_missing_id")
            if "status_id" in item:
                item_status_id = _positive_int(item["status_id"])
                if item_status_id is None:
                    raise ContractDriftError("kwork.item_status_id_invalid")
                if item_status_id != group_id:
                    raise ContractDriftError("kwork.item_status_group_mismatch")
            title = item.get("title") or item.get("name")
            return KworkRecord(
                kwork_id=kwork_id,
                title=title if isinstance(title, str) else None,
                status_group_id=group_id,
                status_group_name=group_name,
                raw=item,
            )

        async def visit_group(group_value: Any) -> None:
            group = _as_json_dict(
                group_value,
                "kwork.group",
                secrets_to_redact=self._redaction_secrets,
            )
            group_id = _positive_int(group.get("id"))
            if group_id is None:
                raise ContractDriftError("kwork.group_missing_id")
            group_name_value = group.get("name")
            if not isinstance(group_name_value, str) or not group_name_value.strip():
                raise ContractDriftError("kwork.group_missing_name")
            group_name = group_name_value
            group_count = group.get("kworks_count")
            if isinstance(group_count, bool) or not isinstance(group_count, int) or group_count < 0:
                raise ContractDriftError("kwork.group_count_invalid")
            embedded_items = group.get("kworks")
            if not isinstance(embedded_items, list):
                raise ContractDriftError("kwork.group.items_not_array")
            if len(embedded_items) > group_count or (group_count == 0 and embedded_items):
                raise ContractDriftError("kwork.group_count_inconsistent")
            if group_count > 0 and not embedded_items:
                raise ContractDriftError("kwork.group_first_page_empty")
            embedded_ids = [
                parse_item(
                    item_value,
                    group_id=group_id,
                    group_name=group_name,
                ).kwork_id
                for item_value in embedded_items
            ]
            if group_count == 0:
                return

            group_records: list[KworkRecord] = []
            total_pages: int | None = None
            for page in range(1, 51):
                params = {
                    "user_id": actor_id,
                    "status_id": group_id,
                    "page": page,
                }
                enforce_route_params("user_kworks", params)

                async def load_page(
                    client: Kwork,
                    bound_params: dict[str, int] = params,
                ) -> Any:
                    return await client.user_kworks(
                        use_token=True,
                        **bound_params,
                    )

                page_data = await self.session.call_read(
                    "user-kworks",
                    load_page,
                )
                page_response = _response(page_data, "userKworks")
                if not isinstance(page_response, list):
                    raise ContractDriftError("userKworks:response_not_array")
                page_records = [
                    parse_item(
                        item_value,
                        group_id=group_id,
                        group_name=group_name,
                    )
                    for item_value in page_response
                ]
                _, limit, total, pages = _strict_paging(
                    page_data,
                    "userKworks",
                    requested_page=page,
                    item_count=len(page_records),
                )
                if limit is None or total is None:
                    raise ContractDriftError("userKworks:paging_incomplete")
                if total != group_count:
                    raise ContractDriftError("userKworks:group_count_mismatch")
                if total_pages is None:
                    total_pages = pages
                    if total_pages > 50:
                        raise ContractDriftError("userKworks:pagination_safety_limit")
                elif pages != total_pages:
                    raise ContractDriftError("userKworks:paging_pages_changed")
                if page == 1 and [record.kwork_id for record in page_records] != embedded_ids:
                    raise ContractDriftError("userKworks:first_page_mismatch")
                group_records.extend(page_records)
                if page >= pages:
                    break
            else:  # pragma: no cover - bounded above by the explicit pages check
                raise ContractDriftError("userKworks:pagination_safety_limit")

            if len(group_records) != group_count:
                raise ContractDriftError("userKworks:group_count_inconsistent")
            for record in group_records:
                if record.kwork_id in seen_ids:
                    raise ContractDriftError("userKworks:duplicate_kwork_id")
                seen_ids.add(record.kwork_id)
                items.append(record)

        for group in raw_response:
            await visit_group(group)
        return ItemCollection[KworkRecord](
            items=items,
            raw_metadata=_page_metadata(
                data,
                secrets_to_redact=self._redaction_secrets,
            ),
        )

    async def get_kwork_details(self, kwork_id: int) -> RawObjectData | None:
        params = {"id": kwork_id}
        enforce_route_params("get_kwork_details_extra", params)
        try:
            data = await self.session.call_read(
                "kwork-details",
                lambda client: client.get_kwork_details_extra(use_token=True, **params),
            )
        except GatewayError as error:
            if error.code is ErrorCode.NOT_FOUND:
                return None
            raise
        raw_response = _response(data, "getKworkDetailsExtra")
        if raw_response is None or raw_response == []:
            return None
        if not isinstance(raw_response, dict | list):
            raise ContractDriftError("getKworkDetailsExtra:unexpected_response")
        raw = sanitize_external(
            raw_response,
            secrets=self._redaction_secrets,
        )
        if not isinstance(raw, dict | list):  # pragma: no cover
            raise ContractDriftError("getKworkDetailsExtra:not_json")
        return RawObjectData(raw=raw)

    def _category_record(self, category: Any) -> CategoryRecord:
        raw = _as_json_dict(
            category,
            "category",
            secrets_to_redact=self._redaction_secrets,
        )
        category_id = _positive_int(raw.get("id"))
        if category_id is None:
            raise ContractDriftError("category:missing_id")
        nested = getattr(category, "subcategories", None) or []
        name = raw.get("name")
        return CategoryRecord(
            category_id=category_id,
            name=name if isinstance(name, str) else None,
            children=[self._category_record(child) for child in nested],
            raw=raw,
        )

    async def list_categories(self) -> ItemCollection[CategoryRecord]:
        categories = await self.session.call_read(
            "categories",
            lambda client: client.get_categories(),
        )
        return ItemCollection[CategoryRecord](items=[self._category_record(category) for category in categories])

    async def list_favorite_categories(self) -> RawObjectData:
        enforce_route_params("favorite_categories", {})
        data = await self.session.call_read(
            "favorite-categories",
            lambda client: client.favorite_categories(use_token=True),
        )
        raw_response = _response(data, "favoriteCategories")
        if not isinstance(raw_response, dict | list):
            raise ContractDriftError("favoriteCategories:unexpected_response")
        raw = sanitize_external(
            raw_response,
            secrets=self._redaction_secrets,
        )
        if not isinstance(raw, dict | list):  # pragma: no cover
            raise ContractDriftError("favoriteCategories:not_json")
        return RawObjectData(raw=raw)

    async def list_notifications(self) -> RawObjectData:
        data = await self.session.call_read(
            "notifications",
            lambda client: client.get_notifications(),
        )
        raw_response = _response(data, "notifications")
        if not isinstance(raw_response, dict | list):
            raise ContractDriftError("notifications:unexpected_response")
        raw = sanitize_external(
            raw_response,
            secrets=self._redaction_secrets,
        )
        if not isinstance(raw, dict | list):  # pragma: no cover
            raise ContractDriftError("notifications:not_json")
        return RawObjectData(raw=raw)

    async def _all_offers(self, max_pages: int = 50) -> list[OfferRecord]:
        results: list[OfferRecord] = []
        for page in range(1, max_pages + 1):
            result = await self.list_my_offers(page)
            results.extend(result.items)
            if result.page is None or result.page.has_more is not True:
                return results
        raise ContractDriftError("offers:pagination_safety_limit")

    async def _all_orders(self, max_pages: int = 50) -> list[OrderRecord]:
        results: list[OrderRecord] = []
        for page in range(1, max_pages + 1):
            result = await self.list_worker_orders(page)
            results.extend(result.items)
            if result.page is None or result.page.has_more is not True:
                return results
        raise ContractDriftError("workerOrders:pagination_safety_limit")

    async def _find_message(
        self,
        *,
        username: str,
        message_id: int,
        max_pages: int = 50,
    ) -> MessageRecord | None:
        inconclusive = False
        for page in range(1, max_pages + 1):
            dialog = await self.get_dialog(username, page)
            if any(item.message_id is None for item in dialog.items):
                inconclusive = True
            match = next(
                (item for item in dialog.items if item.message_id == message_id),
                None,
            )
            if match is not None:
                return match
            if dialog.page is None or dialog.page.has_more is not True:
                if inconclusive:
                    raise AmbiguousWriteError("message_lookup_missing_stable_id")
                return None
        raise ContractDriftError("dialog:pagination_safety_limit")

    async def _find_dialog_by_user_id(
        self,
        user_id: int,
        *,
        max_pages: int = 50,
    ) -> DialogRecord | None:
        inconclusive = False
        for page in range(1, max_pages + 1):
            dialogs = await self.list_dialogs(page)
            if any(item.user_id is None for item in dialogs.items):
                inconclusive = True
            match = next(
                (item for item in dialogs.items if item.user_id == user_id),
                None,
            )
            if match is not None:
                return match
            if dialogs.page is None or dialogs.page.has_more is not True:
                if inconclusive:
                    raise AmbiguousWriteError("dialog_lookup_missing_stable_user_id")
                return None
        raise ContractDriftError("dialogs:pagination_safety_limit")

    async def _matching_sent_messages(
        self,
        *,
        username: str,
        text: str,
        sender_id: int,
        prepared_at: float,
        unknown_at: float,
        max_pages: int = 50,
    ) -> list[MessageRecord]:
        matches: list[MessageRecord] = []
        inconclusive = False
        conflicting_candidate = False
        lower_bound = prepared_at - 60.0
        upper_bound = unknown_at + 300.0
        for page in range(1, max_pages + 1):
            dialog = await self.get_dialog(username, page)
            for message in dialog.items:
                if message.text is None:
                    inconclusive = True
                    continue
                text_matches = _normalize_remote_text(message.text) == _normalize_remote_text(text)
                timestamp = _epoch_seconds(message.created_at)
                if timestamp is None or not lower_bound <= timestamp <= upper_bound:
                    if text_matches and timestamp is None:
                        inconclusive = True
                    continue
                if message.message_id is None or message.sender_id is None:
                    inconclusive = True
                    continue
                if message.sender_id != sender_id:
                    continue
                if text_matches:
                    matches.append(message)
                else:
                    conflicting_candidate = True
            if dialog.page is None or dialog.page.has_more is not True:
                if not matches and (inconclusive or conflicting_candidate):
                    raise AmbiguousWriteError("message_readback_inconclusive")
                return matches
        raise ContractDriftError("dialog:pagination_safety_limit")

    @staticmethod
    def _offer_fingerprint_state(
        offer: OfferRecord,
        request: dict[str, Any],
        *,
        prepared_at: float | None = None,
        unknown_at: float | None = None,
    ) -> Literal["match", "different", "inconclusive"]:
        def normalized_text(value: Any) -> Any:
            if not isinstance(value, str):
                return value
            return _normalize_remote_text(value)

        expected = (
            (normalized_text(offer.title), normalized_text(request.get("title"))),
            (normalized_text(offer.description), normalized_text(request.get("description"))),
            (offer.price, request.get("price")),
            (offer.duration_days, request.get("duration_days")),
        )
        if any(actual is not None and actual != wanted for actual, wanted in expected):
            return "different"
        if any(actual is None for actual, _wanted in expected):
            return "inconclusive"
        if prepared_at is not None and unknown_at is not None:
            created_at = _epoch_seconds(offer.created_at)
            if created_at is None:
                return "inconclusive"
            if not prepared_at - 60.0 <= created_at <= unknown_at + 300.0:
                return "different"
        return "match"

    async def _matching_offers(
        self,
        request: dict[str, Any],
        *,
        record: StoredWrite | None = None,
    ) -> list[OfferRecord]:
        project_id = int(request["project_id"])
        matches: list[OfferRecord] = []
        inconclusive = False
        conflicting_candidate = False
        for listed_offer in await self._all_offers():
            if listed_offer.project_id != project_id:
                continue
            offer = listed_offer
            if any(
                value is None
                for value in (
                    offer.title,
                    offer.description,
                    offer.price,
                    offer.duration_days,
                )
            ):
                detailed = await self.get_offer(offer.offer_id)
                if detailed is None:
                    inconclusive = True
                    continue
                offer = detailed
            state = self._offer_fingerprint_state(
                offer,
                request,
                prepared_at=record.prepared_at if record is not None else None,
                unknown_at=record.updated_at if record is not None else None,
            )
            if state == "match":
                matches.append(offer)
            elif state == "inconclusive":
                inconclusive = True
            else:
                conflicting_candidate = True
        if not matches and conflicting_candidate:
            raise AmbiguousWriteError("offer_readback_conflicting_candidate")
        if not matches and inconclusive:
            raise AmbiguousWriteError("offer_readback_inconclusive")
        return matches

    async def _resolve_user_id(self, username: str) -> int:
        user = await self.get_user(user_id=None, username=username)
        if user is None:
            raise GatewayError(ErrorCode.NOT_FOUND, diagnostic="recipient_not_found")
        return user.user_id

    async def _preflight(
        self,
        request: WriteRequest,
        resolved: dict[str, JsonValue],
    ) -> None:
        if request.action is WriteAction.SUBMIT_OFFER:
            project = await self.get_project(request.project_id)
            if project is None:
                raise GatewayError(ErrorCode.NOT_FOUND, diagnostic="project_not_found")
            status = str(project.status or "").casefold()
            if any(value in status for value in ("closed", "archived", "stopped", "закры")):
                raise GatewayError(ErrorCode.CLOSED_PROJECT, diagnostic=f"status={status}")
            if any(offer.project_id == request.project_id for offer in await self._all_offers()):
                raise GatewayError(ErrorCode.DUPLICATE, diagnostic="offer_exists_for_project")
            connects = await self.get_connects()
            if connects.active <= 0:
                raise GatewayError(
                    ErrorCode.INSUFFICIENT_CONNECTS,
                    diagnostic="active_connects=0",
                )
            resolved["project_status_at_prepare"] = project.status
            resolved["connects_at_prepare"] = connects.active
        elif request.action is WriteAction.DELETE_OFFER:
            offer_id = cast(Any, request).offer_id
            if await self.get_offer(offer_id) is None:
                raise GatewayError(ErrorCode.NOT_FOUND, diagnostic="offer_not_found")
        elif request.action is WriteAction.SEND_MESSAGE:
            if request.user_id is not None:
                resolved["user_id"] = request.user_id
            else:
                resolved["user_id"] = await self._resolve_user_id(cast(str, request.username))
        elif request.action in {
            WriteAction.EDIT_MESSAGE,
            WriteAction.DELETE_MESSAGE,
        }:
            message = await self._find_message(
                username=request.username,
                message_id=request.message_id,
            )
            if message is None:
                raise GatewayError(
                    ErrorCode.NOT_FOUND,
                    diagnostic="message_not_found_in_expected_dialog",
                )
            resolved["message_sender_id_at_prepare"] = message.sender_id
        elif request.action is WriteAction.MARK_DIALOG_READ:
            dialog = await self._find_dialog_by_user_id(request.user_id)
            if dialog is None:
                raise GatewayError(ErrorCode.NOT_FOUND, diagnostic="dialog_not_found")
            resolved["dialog_username_at_prepare"] = dialog.username
        elif request.action is WriteAction.SUBMIT_ORDER_APPROVAL:
            order_id = cast(Any, request).order_id
            orders = await self._all_orders()
            order = next((item for item in orders if item.order_id == order_id), None)
            if order is None:
                raise GatewayError(ErrorCode.NOT_FOUND, diagnostic="worker_order_not_found")
            normalized_status = _optional_int(order.status)
            if normalized_status is None:
                raise ContractDriftError("worker_order_status_not_numeric")
            if normalized_status != 1:
                raise GatewayError(
                    ErrorCode.VALIDATION,
                    diagnostic=f"order_status={normalized_status}",
                )
            resolved["order_status_at_prepare"] = normalized_status
        elif request.action is WriteAction.SET_KWORK_STATE:
            records = await self.list_my_kworks()
            kwork = next(
                (item for item in records.items if item.kwork_id == request.kwork_id),
                None,
            )
            if kwork is None:
                raise GatewayError(ErrorCode.NOT_FOUND, diagnostic="kwork_not_found")
            current = (kwork.status_group_name or "").casefold()
            if request.target_state == "active" and "актив" in current:
                raise GatewayError(ErrorCode.DUPLICATE, diagnostic="kwork_already_active")
            if request.target_state == "paused" and ("пауз" in current or "останов" in current):
                raise GatewayError(ErrorCode.DUPLICATE, diagnostic="kwork_already_paused")

    async def prepare_write(
        self,
        request: WriteRequest,
        idempotency_key: str,
        *,
        correlation_id: str,
    ) -> WriteStatusData:
        actor = await self.session.verify_write_identity()
        request_json = request.model_dump(mode="json")
        action = WriteAction(request_json["action"])
        existing = await self.coordinator.get_write_by_idempotency(
            scope=self.session.scope,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            existing_payload = json.loads(existing.payload_json)
            if not isinstance(existing_payload, dict) or existing_payload.get("request") != request_json:
                raise GatewayError(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    diagnostic="existing_input_differs",
                )
            prepared = await self.coordinator.prepare_write(
                scope=self.session.scope,
                idempotency_key=idempotency_key,
                action=action,
                payload=existing_payload,
            )
            return self._write_status(
                prepared.record,
                correlation_id=correlation_id,
                confirmation_token=prepared.confirmation_token,
            )

        resolved: dict[str, JsonValue] = {}
        await self._preflight(request, resolved)
        payload: dict[str, Any] = {
            "request": request_json,
            "resolved": resolved,
            "prepared_account_id": actor.id,
        }
        prepared = await self.coordinator.prepare_write(
            scope=self.session.scope,
            idempotency_key=idempotency_key,
            action=action,
            payload=payload,
        )
        return self._write_status(
            prepared.record,
            correlation_id=correlation_id,
            confirmation_token=prepared.confirmation_token,
        )

    def _write_status(
        self,
        record: StoredWrite,
        *,
        correlation_id: str,
        confirmation_token: str | None = None,
    ) -> WriteStatusData:
        try:
            payload = json.loads(record.payload_json)
        except json.JSONDecodeError as exc:  # pragma: no cover - protected DB invariant
            raise ContractDriftError("stored_write_payload_invalid") from exc
        if not isinstance(payload, dict):
            raise ContractDriftError("stored_write_payload_not_object")
        terminal_error = None
        if record.error_json:
            try:
                terminal_error = ErrorInfo.model_validate_json(record.error_json)
            except ValueError:
                if record.state is WriteState.SUBMISSION_UNKNOWN:
                    terminal_error = AmbiguousWriteError("stored_ambiguous").to_info(correlation_id)
        result = None
        if record.result_json:
            parsed_result = json.loads(record.result_json)
            if isinstance(parsed_result, dict):
                result = cast(
                    dict[str, JsonValue],
                    sanitize_external(
                        parsed_result,
                        secrets=self._redaction_secrets,
                    ),
                )
        return WriteStatusData(
            write_id=record.write_id,
            idempotency_key=record.idempotency_key,
            action=record.action,
            state=record.state,
            payload_hash=record.payload_hash,
            payload=cast(
                dict[str, JsonValue],
                sanitize_external(
                    payload,
                    secrets=self._redaction_secrets,
                ),
            ),
            prepared_at=datetime.fromtimestamp(record.prepared_at, tz=UTC),
            expires_at=datetime.fromtimestamp(record.expires_at, tz=UTC),
            updated_at=datetime.fromtimestamp(record.updated_at, tz=UTC),
            can_commit=record.state is WriteState.PREPARED,
            reconciliation_required=record.state is WriteState.SUBMISSION_UNKNOWN,
            confirmation_token=confirmation_token,
            result=result,
            terminal_error=terminal_error,
        )

    async def _finish_remote_outcome(
        self,
        *,
        write_id: str,
        scope: str,
        state: WriteState,
        correlation_id: str,
        result: dict[str, JsonValue] | None = None,
        error: ErrorInfo | None = None,
        note: str,
    ) -> StoredWrite:
        """Persist a remote outcome or conservatively preserve ambiguity.

        Once a remote write may have started, a local ledger failure must never
        leave the caller with a retryable/internal-looking result. A second,
        best-effort transition to ``submission_unknown`` handles transient
        storage failures; if storage remains unavailable, the typed exception
        still requires reconciliation and the stale committing lease can be
        recovered later.
        """

        try:
            return await self.coordinator.finish_write(
                write_id=write_id,
                scope=scope,
                owner=self.instance_id,
                state=state,
                result=result,
                error=error,
                note=note,
            )
        except Exception:
            try:
                current = await self.coordinator.get_write(write_id, scope=scope)
            except Exception:
                current = None
            if current is not None and current.state in {
                WriteState.SUCCEEDED,
                WriteState.FAILED_KNOWN,
                WriteState.SUBMISSION_UNKNOWN,
            }:
                return current

            ambiguous = AmbiguousWriteError("remote_outcome_ledger_failure")
            try:
                return await self.coordinator.finish_write(
                    write_id=write_id,
                    scope=scope,
                    owner=self.instance_id,
                    state=WriteState.SUBMISSION_UNKNOWN,
                    error=ambiguous.to_info(correlation_id),
                    note="ledger_failure_after_remote_write",
                )
            except Exception as fallback_error:
                try:
                    current = await self.coordinator.get_write(write_id, scope=scope)
                except Exception:
                    current = None
                if current is not None and current.state in {
                    WriteState.SUCCEEDED,
                    WriteState.FAILED_KNOWN,
                    WriteState.SUBMISSION_UNKNOWN,
                }:
                    return current
                raise ambiguous from fallback_error

    async def _await_durable_ledger[ResultT](
        self,
        operation: Awaitable[ResultT],
    ) -> ResultT:
        result, cancelled = await _finish_shielded_task(asyncio.ensure_future(operation))
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def _settle_cancelled_claim(
        self,
        *,
        write_id: str,
        scope: str,
        correlation_id: str,
        remote_started: bool,
    ) -> None:
        async def persist_unknown() -> StoredWrite:
            ambiguous = AmbiguousWriteError("cancelled_after_remote_boundary")
            return await self._finish_remote_outcome(
                write_id=write_id,
                scope=scope,
                state=WriteState.SUBMISSION_UNKNOWN,
                correlation_id=correlation_id,
                error=ambiguous.to_info(correlation_id),
                note="cancelled_after_remote_boundary",
            )

        if remote_started:
            await _finish_shielded_task(asyncio.ensure_future(persist_unknown()))
            return
        try:
            await _finish_shielded_task(
                asyncio.ensure_future(
                    self.coordinator.release_write_claim(
                        write_id=write_id,
                        scope=scope,
                        owner=self.instance_id,
                        note="cancelled_before_remote_boundary",
                    )
                )
            )
            return
        except AmbiguousWriteError:
            current, _ = await _finish_shielded_task(
                asyncio.ensure_future(self.coordinator.get_write(write_id, scope=scope))
            )
            if current is not None and current.state in {
                WriteState.SUCCEEDED,
                WriteState.FAILED_KNOWN,
                WriteState.SUBMISSION_UNKNOWN,
                WriteState.EXPIRED,
            }:
                return
            if current is not None and current.state is WriteState.COMMITTING and current.remote_started_at is not None:
                await _finish_shielded_task(asyncio.ensure_future(persist_unknown()))
                return
            raise

    async def _settle_cancelled_claim_safely(
        self,
        *,
        write_id: str,
        scope: str,
        correlation_id: str,
        remote_started: bool,
    ) -> None:
        """Best-effort settlement that never replaces the caller's cancellation."""

        try:
            await self._settle_cancelled_claim(
                write_id=write_id,
                scope=scope,
                correlation_id=correlation_id,
                remote_started=remote_started,
            )
        except Exception as exc:
            # The durable marker remains authoritative: stale recovery returns a
            # pre-remote claim to prepared, or makes a post-boundary claim
            # submission_unknown.  Log only the exception type, never payloads.
            logger.error(
                "write_cancellation_settlement_failed remote_started={} exception_type={}",
                remote_started,
                type(exc).__name__,
            )

    async def get_write_status(
        self,
        write_id: str,
        *,
        correlation_id: str,
    ) -> WriteStatusData | None:
        record = await self._load_write_record(write_id, scope=self.session.scope)
        if record is None:
            return None
        return self._write_status(record, correlation_id=correlation_id)

    async def _load_write_record(
        self,
        write_id: str,
        *,
        scope: str,
    ) -> StoredWrite | None:
        record = await self.coordinator.get_write(write_id, scope=scope)
        if (
            record is None
            or record.state is not WriteState.COMMITTING
            or record.lease_expires is None
            or record.lease_expires > time.time()
        ):
            return record
        try:
            async with self.coordinator.writer_guard(scope):
                return await self.coordinator.recover_stale_write(
                    write_id=write_id,
                    scope=scope,
                )
        except GatewayError as error:
            if error.code is ErrorCode.WRITE_IN_PROGRESS:
                return record
            raise

    async def commit_write(
        self,
        *,
        write_id: str,
        payload_hash: str,
        confirmation_token: str,
        correlation_id: str,
    ) -> WriteStatusData:
        scope = self.session.scope
        record = await self.coordinator.get_write(write_id, scope=scope)
        if record is None:
            raise GatewayError(ErrorCode.NOT_FOUND, diagnostic="write_id_not_found")
        async with self.coordinator.writer_guard(scope):
            return await self._commit_write_guarded(
                scope=scope,
                write_id=write_id,
                payload_hash=payload_hash,
                confirmation_token=confirmation_token,
                correlation_id=correlation_id,
            )

    async def _commit_write_guarded(
        self,
        *,
        scope: str,
        write_id: str,
        payload_hash: str,
        confirmation_token: str,
        correlation_id: str,
    ) -> WriteStatusData:
        try:
            claimed, cancelled_during_claim = await _finish_shielded_task(
                asyncio.ensure_future(
                    self.coordinator.claim_write(
                        write_id=write_id,
                        scope=scope,
                        payload_hash=payload_hash,
                        confirmation_token=confirmation_token,
                        owner=self.instance_id,
                    )
                )
            )
        except AmbiguousWriteError:
            record = await self.coordinator.get_write(write_id, scope=scope)
            if record is None:
                raise
            return self._write_status(record, correlation_id=correlation_id)
        if claimed.state is not WriteState.COMMITTING:
            if cancelled_during_claim:
                raise asyncio.CancelledError
            return self._write_status(claimed, correlation_id=correlation_id)
        if cancelled_during_claim:
            await self._settle_cancelled_claim_safely(
                write_id=write_id,
                scope=scope,
                correlation_id=correlation_id,
                remote_started=claimed.remote_started_at is not None,
            )
            raise asyncio.CancelledError

        try:
            actor = await self.session.verify_write_identity()
            stored_payload = json.loads(claimed.payload_json)
            if not isinstance(stored_payload, dict):
                raise ContractDriftError("stored_write_payload_not_object")
            prepared_account_id = _positive_int(stored_payload.get("prepared_account_id"))
            if prepared_account_id is None:
                raise ContractDriftError("stored_write_missing_prepared_account")
            if actor.id != prepared_account_id:
                raise GatewayError(
                    ErrorCode.ACCOUNT_MISMATCH,
                    diagnostic="prepared_account_changed_before_commit",
                )
            request_data, stored_resolved = self._decode_request(claimed)
            try:
                request_model = _WRITE_REQUEST_ADAPTER.validate_python(request_data)
            except ValidationError as exc:
                raise ContractDriftError("stored_write_request_invalid") from exc
            fresh_resolved: dict[str, JsonValue] = {}
            await self._preflight(request_model, fresh_resolved)
            if claimed.action is WriteAction.SEND_MESSAGE and fresh_resolved.get("user_id") != stored_resolved.get(
                "user_id"
            ):
                raise GatewayError(
                    ErrorCode.CONTRACT_DRIFT,
                    diagnostic="recipient_identity_changed_since_prepare",
                )
        except asyncio.CancelledError:
            await self._settle_cancelled_claim_safely(
                write_id=write_id,
                scope=scope,
                correlation_id=correlation_id,
                remote_started=False,
            )
            raise
        except GatewayError as error:
            if error.retryable or error.code in {
                ErrorCode.AUTH_REQUIRED,
                ErrorCode.AUTH_EXPIRED,
                ErrorCode.ACCOUNT_BINDING_REQUIRED,
                ErrorCode.ACCOUNT_MISMATCH,
                ErrorCode.WRITE_DISABLED,
            }:
                await self._await_durable_ledger(
                    self.coordinator.release_write_claim(
                        write_id=write_id,
                        scope=scope,
                        owner=self.instance_id,
                        note="read_only_commit_preflight_failed",
                    )
                )
                raise
            finished = await self._await_durable_ledger(
                self.coordinator.finish_write(
                    write_id=write_id,
                    scope=scope,
                    owner=self.instance_id,
                    state=WriteState.FAILED_KNOWN,
                    error=error.to_info(correlation_id),
                    note="definitive_commit_preflight_failure",
                )
            )
            return self._write_status(finished, correlation_id=correlation_id)

        remote_started = asyncio.Event()

        async def before_remote_attempt() -> None:
            if remote_started.is_set():
                return
            _, cancelled = await _finish_shielded_task(
                asyncio.ensure_future(
                    self.coordinator.mark_write_remote_started(
                        write_id=write_id,
                        scope=scope,
                        owner=self.instance_id,
                    )
                )
            )
            remote_started.set()
            if cancelled:
                raise asyncio.CancelledError

        async def settle_failure_before_remote_boundary() -> StoredWrite:
            try:
                return await self._await_durable_ledger(
                    self.coordinator.release_write_claim(
                        write_id=write_id,
                        scope=scope,
                        owner=self.instance_id,
                        note="failure_before_remote_boundary",
                    )
                )
            except AmbiguousWriteError as marker_error:
                # A fault can occur after the marker transaction commits but
                # before its callback returns.  Re-read the durable marker and
                # conservatively preserve unknown rather than retrying.
                await self._settle_cancelled_claim(
                    write_id=write_id,
                    scope=scope,
                    correlation_id=correlation_id,
                    remote_started=False,
                )
                current = await self.coordinator.get_write(write_id, scope=scope)
                if current is None:
                    raise ContractDriftError("claimed_write_disappeared") from marker_error
                return current

        try:
            async with self.session.exclusive_client():
                result = await self._execute_write(
                    claimed,
                    before_remote_attempt=before_remote_attempt,
                )
        except asyncio.CancelledError:
            await self._settle_cancelled_claim_safely(
                write_id=write_id,
                scope=scope,
                correlation_id=correlation_id,
                remote_started=remote_started.is_set(),
            )
            raise
        except AmbiguousWriteError as error:
            if not remote_started.is_set():
                settled = await settle_failure_before_remote_boundary()
                if settled.state is not WriteState.PREPARED:
                    return self._write_status(settled, correlation_id=correlation_id)
                raise
            finished = await self._await_durable_ledger(
                self._finish_remote_outcome(
                    write_id=write_id,
                    scope=scope,
                    state=WriteState.SUBMISSION_UNKNOWN,
                    correlation_id=correlation_id,
                    error=error.to_info(correlation_id),
                    note="ambiguous_remote_result",
                )
            )
            return self._write_status(finished, correlation_id=correlation_id)
        except GatewayError as error:
            if not remote_started.is_set():
                settled = await settle_failure_before_remote_boundary()
                if settled.state is not WriteState.PREPARED:
                    return self._write_status(settled, correlation_id=correlation_id)
                raise
            if error.code in {
                ErrorCode.TIMEOUT,
                ErrorCode.PROXY,
                ErrorCode.UPSTREAM_UNAVAILABLE,
                ErrorCode.CONTRACT_DRIFT,
                ErrorCode.INTERNAL,
            }:
                ambiguous = AmbiguousWriteError(error.diagnostic)
                finished = await self._await_durable_ledger(
                    self._finish_remote_outcome(
                        write_id=write_id,
                        scope=scope,
                        state=WriteState.SUBMISSION_UNKNOWN,
                        correlation_id=correlation_id,
                        error=ambiguous.to_info(correlation_id),
                        note="transient_after_commit_start",
                    )
                )
            else:
                finished = await self._await_durable_ledger(
                    self._finish_remote_outcome(
                        write_id=write_id,
                        scope=scope,
                        state=WriteState.FAILED_KNOWN,
                        correlation_id=correlation_id,
                        error=error.to_info(correlation_id),
                        note="definitive_failure",
                    )
                )
            return self._write_status(finished, correlation_id=correlation_id)
        except Exception as exc:
            if not remote_started.is_set():
                settled = await settle_failure_before_remote_boundary()
                if settled.state is not WriteState.PREPARED:
                    return self._write_status(settled, correlation_id=correlation_id)
                raise
            ambiguous = AmbiguousWriteError(type(exc).__name__)
            finished = await self._await_durable_ledger(
                self._finish_remote_outcome(
                    write_id=write_id,
                    scope=scope,
                    state=WriteState.SUBMISSION_UNKNOWN,
                    correlation_id=correlation_id,
                    error=ambiguous.to_info(correlation_id),
                    note="unexpected_error_after_remote_write_started",
                )
            )
            return self._write_status(finished, correlation_id=correlation_id)

        if not remote_started.is_set():
            settled = await settle_failure_before_remote_boundary()
            if settled.state is not WriteState.PREPARED:
                return self._write_status(settled, correlation_id=correlation_id)
            raise ContractDriftError("write_execution_without_remote_boundary")
        finished = await self._await_durable_ledger(
            self._finish_remote_outcome(
                write_id=write_id,
                scope=scope,
                state=WriteState.SUCCEEDED,
                correlation_id=correlation_id,
                result=result,
                note="remote_success_confirmed",
            )
        )
        return self._write_status(finished, correlation_id=correlation_id)

    def _decode_request(self, record: StoredWrite) -> tuple[dict[str, Any], dict[str, Any]]:
        payload = json.loads(record.payload_json)
        if not isinstance(payload, dict):
            raise ContractDriftError("write_payload_not_object")
        request = payload.get("request")
        resolved = payload.get("resolved")
        if not isinstance(request, dict) or not isinstance(resolved, dict):
            raise ContractDriftError("write_payload_shape")
        if request.get("action") != record.action.value:
            raise ContractDriftError("write_action_mismatch")
        return request, resolved

    async def _execute_write(
        self,
        record: StoredWrite,
        *,
        before_remote_attempt: Callable[[], Awaitable[None]] | None = None,
    ) -> dict[str, JsonValue]:
        request, resolved = self._decode_request(record)
        action = record.action
        if action is WriteAction.SUBMIT_OFFER:
            return await self._execute_submit_offer(
                request,
                before_remote_attempt=before_remote_attempt,
            )
        if action is WriteAction.DELETE_OFFER:
            delete_offer_params = {"id": request["offer_id"]}
            enforce_route_params("delete_offer", delete_offer_params)
            data = await self.session.call_write_step(
                "write-delete-offer",
                lambda client: client.delete_offer(
                    use_token=True,
                    retry=False,
                    **delete_offer_params,
                ),
                before_remote_attempt=before_remote_attempt,
            )
            _response(data, "deleteOffer", require_response=False)
            return {"offer_id": request["offer_id"], "deleted": True}
        if action is WriteAction.SEND_MESSAGE:
            user_id = _positive_int(resolved.get("user_id"))
            if user_id is None:
                raise ContractDriftError("send_message:missing_resolved_user_id")
            data = await self.session.call_write_step(
                "write-send-message",
                lambda client: client.send_message(user_id=user_id, text=request["text"]),
                before_remote_attempt=before_remote_attempt,
            )
            raw_response = _response(data, "inboxCreate")
            raw = _as_json_dict(
                raw_response,
                "inboxCreate.response",
                secrets_to_redact=self._redaction_secrets,
            )
            message_id = _positive_int(raw.get("id"))
            if message_id is None:
                raise AmbiguousWriteError("inboxCreate_missing_message_id")
            return {"message_id": message_id, "user_id": user_id}
        if action is WriteAction.EDIT_MESSAGE:
            edit_message_params = {"id": request["message_id"]}
            enforce_route_params("inbox_edit_query", edit_message_params)
            data = await self.session.call_write_step(
                "write-edit-message",
                lambda client: client.inbox_edit(
                    use_token=True,
                    retry=False,
                    body={"text": request["text"]},
                    **edit_message_params,
                ),
                before_remote_attempt=before_remote_attempt,
            )
            _response(data, "inboxEdit", require_response=False)
            return {"message_id": request["message_id"], "edited": True}
        if action is WriteAction.DELETE_MESSAGE:
            data = await self.session.call_write_step(
                "write-delete-message",
                lambda client: client.delete_message(request["message_id"]),
                before_remote_attempt=before_remote_attempt,
            )
            _response(data, "inboxDelete", require_response=False)
            return {"message_id": request["message_id"], "deleted": True}
        if action is WriteAction.MARK_DIALOG_READ:
            mark_read_params = {"user_id": request["user_id"]}
            enforce_route_params("inbox_read", mark_read_params)
            data = await self.session.call_write_step(
                "write-mark-dialog-read",
                lambda client: client.inbox_read(
                    use_token=True,
                    retry=False,
                    **mark_read_params,
                ),
                before_remote_attempt=before_remote_attempt,
            )
            _response(data, "inboxRead", require_response=False)
            return {"user_id": request["user_id"], "read": True}
        if action is WriteAction.SUBMIT_ORDER_APPROVAL:
            approval_params: dict[str, Any] = {"orderId": request["order_id"]}
            if request.get("metrics"):
                approval_params["metrics[]"] = request["metrics"]
            if request.get("stage_ids"):
                approval_params["stageIds[]"] = request["stage_ids"]
            if request.get("file_ids"):
                approval_params["filesIds[]"] = request["file_ids"]
            enforce_route_params("send_order_for_approval", approval_params)
            data = await self.session.call_write_step(
                "write-order-approval",
                lambda client: client.send_order_for_approval(
                    use_token=True,
                    retry=False,
                    **approval_params,
                ),
                before_remote_attempt=before_remote_attempt,
            )
            _response(data, "sendOrderForApproval", require_response=False)
            return {"order_id": request["order_id"], "submitted_for_approval": True}
        if action is WriteAction.SET_KWORK_STATE:
            kwork_state_params = {"kwork_id": request["kwork_id"]}
            route = "start_kwork" if request["target_state"] == "active" else "pause_kwork"
            enforce_route_params(route, kwork_state_params)
            if route == "start_kwork":
                data = await self.session.call_write_step(
                    "write-start-kwork",
                    lambda client: client.start_kwork(
                        use_token=True,
                        retry=False,
                        **kwork_state_params,
                    ),
                    before_remote_attempt=before_remote_attempt,
                )
                endpoint = "startKwork"
            else:
                data = await self.session.call_write_step(
                    "write-pause-kwork",
                    lambda client: client.pause_kwork(
                        use_token=True,
                        retry=False,
                        **kwork_state_params,
                    ),
                    before_remote_attempt=before_remote_attempt,
                )
                endpoint = "pauseKwork"
            _response(data, endpoint, require_response=False)
            return {
                "kwork_id": request["kwork_id"],
                "state": request["target_state"],
            }
        raise ContractDriftError(f"unimplemented_write_action:{action}")

    @staticmethod
    def _extract_csrf(html: str, client: Kwork) -> str | None:
        cookies = client.session.cookie_jar.filter_cookies(URL(client.web.base_url))
        if "csrf_user_token" in cookies:
            return cookies["csrf_user_token"].value
        if "XSRF-TOKEN" in cookies:
            return unquote(cookies["XSRF-TOKEN"].value)
        patterns = (
            r'csrf_user_token["\']?\s*[:=]\s*["\']([a-f0-9]{16,128})["\']',
            r'name=["\']csrftoken["\']\s+value=["\']([a-f0-9]{16,128})["\']',
        )
        for pattern in patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _draft_key(html: str) -> str:
        patterns = (
            r'draftKey["\']?\s*[:=]\s*["\']([a-z0-9]{6,64})["\']',
            r'name=["\']draftKey["\']\s+value=["\']([a-z0-9]{6,64})["\']',
            r'data-draft-key=["\']([a-z0-9]{6,64})["\']',
        )
        for pattern in patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                return match.group(1)
        alphabet = string.ascii_lowercase + string.digits
        return "".join(secrets.choice(alphabet) for _ in range(8))

    @staticmethod
    def _extract_offer_id(payload: dict[str, Any]) -> int | None:
        direct = _positive_int(payload.get("id")) or _positive_int(payload.get("offer_id"))
        if direct is not None:
            return direct
        response = payload.get("response")
        if isinstance(response, dict):
            return _positive_int(response.get("id")) or _positive_int(response.get("offer_id"))
        if isinstance(response, list) and len(response) == 1 and isinstance(response[0], dict):
            return _positive_int(response[0].get("id")) or _positive_int(response[0].get("offer_id"))
        return None

    @staticmethod
    def _require_web_prerequisite(response: Any, step: str) -> None:
        if not isinstance(response, dict):
            raise ContractDriftError(f"{step}:web_response_not_object")
        status = _optional_int(response.get("status"))
        if status is None:
            raise ContractDriftError(f"{step}:web_response_missing_status")
        if 200 <= status < 300:
            return
        payload = response.get("json")
        classified = classify_upstream_error(
            KworkHTTPException(
                "Kwork web prerequisite failed",
                status=status,
                endpoint=step,
                response_json=payload if isinstance(payload, dict) else None,
            )
        )
        raise classified

    async def _execute_submit_offer(
        self,
        request: dict[str, Any],
        *,
        before_remote_attempt: Callable[[], Awaitable[None]] | None = None,
    ) -> dict[str, JsonValue]:
        client = await self.session.ensure_web_client()
        project_id = int(request["project_id"])
        referer = urljoin(client.web.base_url, f"new_offer?project={project_id}")
        page = await self.session.call_write_step(
            "write-offer-page",
            lambda current: current.web.open_new_offer_page(project_id=project_id),
            before_remote_attempt=before_remote_attempt,
        )
        status = _optional_int(page.get("status"))
        if status not in {200, 302}:
            raise GatewayError(ErrorCode.CSRF, diagnostic=f"offer_page_status={status}")
        page_text = page.get("text")
        html = page_text if isinstance(page_text, str) else ""
        csrf = self._extract_csrf(html, client)
        if not csrf:
            raise GatewayError(ErrorCode.CSRF, diagnostic="csrf_cookie_missing")
        draft_key = self._draft_key(html)

        faq_result = await self.session.call_write_step(
            "write-offer-faq",
            lambda current: current.web.quick_faq_init(referer=referer, page="new_offer"),
            before_remote_attempt=before_remote_attempt,
        )
        self._require_web_prerequisite(faq_result, "quick-faq/init")
        draft_result = await self.session.call_write_step(
            "write-offer-draft",
            lambda current: current.web.create_offer_draft(
                project_id=project_id,
                csrftoken=csrf,
                draft_key=draft_key,
                message="",
                referer=referer,
            ),
            before_remote_attempt=before_remote_attempt,
        )
        self._require_web_prerequisite(draft_result, "wants/create_offer_draft")
        template_result = await self.session.call_write_step(
            "write-offer-template-check",
            lambda current: current.web.check_is_template(
                want_id=project_id,
                description=request["description"],
                referer=referer,
            ),
            before_remote_attempt=before_remote_attempt,
        )
        self._require_web_prerequisite(template_result, "projects/check_is_template")
        try:
            result = await self.session.call_write_step(
                "write-offer-final",
                lambda current: current.web.create_exchange_offer(
                    want_id=project_id,
                    offer_type="custom",
                    description=request["description"],
                    kwork_duration=request["duration_days"],
                    kwork_price=request["price"],
                    kwork_name=request["title"],
                    referer=referer,
                    raise_on_error=False,
                ),
                before_remote_attempt=before_remote_attempt,
            )
        except GatewayError as error:
            if error.code in {
                ErrorCode.TIMEOUT,
                ErrorCode.PROXY,
                ErrorCode.UPSTREAM_UNAVAILABLE,
                ErrorCode.RATE_LIMIT,
            }:
                raise AmbiguousWriteError(error.diagnostic) from error
            raise

        status = _optional_int(result.get("status"))
        payload = result.get("json")
        if status is None or not 200 <= status < 300:
            raise AmbiguousWriteError(f"offer_http_status={status}")
        if not isinstance(payload, dict) or payload.get("success") is not True:
            if isinstance(payload, dict) and payload.get("success") is False:
                api_error = KworkHTTPException(
                    "Kwork rejected offer",
                    status=status,
                    endpoint="api/offer/createoffer",
                    response_json=payload,
                )
                classified = classify_upstream_error(api_error)
                if classified.code in {
                    ErrorCode.DUPLICATE,
                    ErrorCode.CLOSED_PROJECT,
                    ErrorCode.INSUFFICIENT_CONNECTS,
                    ErrorCode.PERMISSION,
                    ErrorCode.CSRF,
                    ErrorCode.CAPTCHA,
                }:
                    raise classified
            raise AmbiguousWriteError("offer_final_invalid_json")
        offer_id = self._extract_offer_id(payload)
        if offer_id is None:
            matches = await self._matching_offers(request)
            if len(matches) == 1:
                offer_id = matches[0].offer_id
            else:
                raise AmbiguousWriteError("offer_success_without_confirmed_id")
        return {
            "offer_id": offer_id,
            "project_id": project_id,
        }

    async def reconcile_write(
        self,
        write_id: str,
        *,
        correlation_id: str,
    ) -> WriteStatusData | None:
        scope = self.session.scope
        record = await self._load_write_record(write_id, scope=scope)
        if record is None:
            return None
        if record.state is not WriteState.SUBMISSION_UNKNOWN:
            return self._write_status(record, correlation_id=correlation_id)
        actor = await self.session.verify_account_identity()
        stored_payload = json.loads(record.payload_json)
        prepared_account_id = (
            _positive_int(stored_payload.get("prepared_account_id")) if isinstance(stored_payload, dict) else None
        )
        if prepared_account_id is None:
            raise ContractDriftError("stored_write_missing_prepared_account")
        if actor.id != prepared_account_id:
            raise GatewayError(
                ErrorCode.ACCOUNT_MISMATCH,
                diagnostic="prepared_account_changed_before_reconcile",
            )
        request, resolved = self._decode_request(record)
        succeeded, result = await self._read_back(record.action, request, resolved, record)
        state = WriteState.RECONCILED_SUCCEEDED if succeeded else WriteState.RECONCILED_ABSENT
        reconciled = await self.coordinator.reconcile_write(
            write_id=write_id,
            scope=scope,
            state=state,
            result=result,
            note="read_back_confirmed" if succeeded else "read_back_absent",
        )
        return self._write_status(reconciled, correlation_id=correlation_id)

    async def _read_back(
        self,
        action: WriteAction,
        request: dict[str, Any],
        resolved: dict[str, Any],
        record: StoredWrite,
    ) -> tuple[bool, dict[str, JsonValue]]:
        if action is WriteAction.SUBMIT_OFFER:
            project_id = int(request["project_id"])
            matches = await self._matching_offers(request, record=record)
            if len(matches) > 1:
                raise AmbiguousWriteError("multiple_matching_offers")
            if matches:
                return True, {
                    "offer_id": matches[0].offer_id,
                    "project_id": project_id,
                    "reconciled": True,
                }
            return False, {"project_id": project_id, "offer_absent": True}
        if action is WriteAction.DELETE_OFFER:
            offer = await self.get_offer(int(request["offer_id"]))
            return (
                offer is None,
                {
                    "offer_id": request["offer_id"],
                    "deleted": offer is None,
                    "reconciled": True,
                },
            )
        if action is WriteAction.SEND_MESSAGE:
            user_id = _positive_int(resolved.get("user_id"))
            if user_id is None:
                raise ContractDriftError("reconcile_message_missing_user_id")
            username = request.get("username")
            if not isinstance(username, str):
                user = await self.get_user(user_id=user_id, username=None)
                username = user.username if user is not None else None
            if not username:
                raise ContractDriftError("reconcile_message_missing_username")
            sender_id = _positive_int(self.config.expected_user_id)
            if sender_id is None:
                raise ContractDriftError("reconcile_message_missing_sender_binding")
            message_matches = await self._matching_sent_messages(
                username=username,
                text=str(request["text"]),
                sender_id=sender_id,
                prepared_at=record.prepared_at,
                unknown_at=record.updated_at,
            )
            if len(message_matches) > 1:
                raise AmbiguousWriteError("multiple_matching_messages")
            if message_matches:
                return True, {
                    "message_id": message_matches[0].message_id,
                    "user_id": user_id,
                    "reconciled": True,
                }
            return False, {"user_id": user_id, "message_absent": True}
        if action in {WriteAction.EDIT_MESSAGE, WriteAction.DELETE_MESSAGE}:
            message = await self._find_message(
                username=str(request["username"]),
                message_id=int(request["message_id"]),
            )
            if action is WriteAction.DELETE_MESSAGE:
                success = message is None
            else:
                if message is not None and message.text is None:
                    raise AmbiguousWriteError("edited_message_text_missing")
                if message is not None and _normalize_remote_text(message.text or "") != _normalize_remote_text(
                    str(request["text"])
                ):
                    raise AmbiguousWriteError("edited_message_text_conflict")
                success = message is not None
            return success, {
                "message_id": request["message_id"],
                "reconciled": True,
                "desired_state_present": success,
            }
        if action is WriteAction.MARK_DIALOG_READ:
            dialog_record = await self._find_dialog_by_user_id(int(request["user_id"]))
            if dialog_record is not None and dialog_record.unread_count is None:
                raise AmbiguousWriteError("dialog_unread_count_missing")
            success = dialog_record is not None and dialog_record.unread_count == 0
            return success, {
                "user_id": request["user_id"],
                "read": success,
                "reconciled": True,
            }
        if action is WriteAction.SUBMIT_ORDER_APPROVAL:
            orders = await self._all_orders()
            order = next(
                (item for item in orders if item.order_id == int(request["order_id"])),
                None,
            )
            normalized_status = _optional_int(order.status) if order is not None else None
            if order is not None and normalized_status is None:
                raise AmbiguousWriteError("worker_order_status_inconclusive")
            if order is not None and normalized_status not in {1, 4}:
                raise AmbiguousWriteError("worker_order_status_changed_inconclusively")
            success = order is not None and normalized_status == 4
            return success, {
                "order_id": request["order_id"],
                "submitted_for_approval": success,
                "reconciled": True,
            }
        if action is WriteAction.SET_KWORK_STATE:
            kworks = await self.list_my_kworks()
            target = next(
                (item for item in kworks.items if item.kwork_id == int(request["kwork_id"])),
                None,
            )
            if target is not None and not target.status_group_name:
                raise AmbiguousWriteError("kwork_status_group_missing")
            group = target.status_group_name.casefold() if target and target.status_group_name else ""
            success = (request["target_state"] == "active" and "актив" in group) or (
                request["target_state"] == "paused" and ("пауз" in group or "останов" in group)
            )
            return success, {
                "kwork_id": request["kwork_id"],
                "state": request["target_state"],
                "reconciled": True,
            }
        raise ContractDriftError(f"reconciliation_not_implemented:{action}")
