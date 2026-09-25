"""Exhaustive paginated scans used by write preflight and reconciliation."""

from __future__ import annotations

from typing import Any, Literal

from kwork_mcp.coordination import StoredWrite
from kwork_mcp.errors import AmbiguousWriteError, ContractDriftError, GatewayError
from kwork_mcp.gateway.parsing import _epoch_seconds, _normalize_remote_text
from kwork_mcp.gateway.reads import ReadOperations
from kwork_mcp.models import (
    DialogRecord,
    ErrorCode,
    MessageRecord,
    OfferRecord,
    OrderRecord,
)


class ReadBackLookups(ReadOperations):
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
