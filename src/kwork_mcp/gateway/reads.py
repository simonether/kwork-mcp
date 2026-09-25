"""Typed read operations over the pinned Kwork API client."""

from __future__ import annotations

from typing import Any, Literal, cast

from kwork import Kwork

from kwork_mcp.contracts import enforce_route_params
from kwork_mcp.coordination import pages_from_paging
from kwork_mcp.errors import ContractDriftError, GatewayError
from kwork_mcp.gateway.base import GatewayBase
from kwork_mcp.gateway.parsing import (
    _as_json_dict,
    _epoch_seconds,
    _fingerprint,
    _optional_int,
    _optional_number,
    _optional_scalar,
    _page_metadata,
    _positive_int,
    _response,
    _strict_paging,
)
from kwork_mcp.models import (
    AccountData,
    CategoryRecord,
    ConnectsData,
    DialogRecord,
    ErrorCode,
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
)
from kwork_mcp.security import sanitize_external


class ReadOperations(GatewayBase):
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
