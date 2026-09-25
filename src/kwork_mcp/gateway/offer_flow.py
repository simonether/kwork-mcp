"""Multi-step web flow that submits an exchange offer."""

from __future__ import annotations

import re
import secrets
import string
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import unquote, urljoin

from kwork import Kwork
from kwork.exceptions import KworkHTTPException
from pydantic import JsonValue
from yarl import URL

from kwork_mcp.errors import AmbiguousWriteError, ContractDriftError, GatewayError, classify_upstream_error
from kwork_mcp.gateway.lookups import ReadBackLookups
from kwork_mcp.gateway.parsing import _optional_int, _positive_int
from kwork_mcp.models import ErrorCode

_WEB_SESSION_ERRORS = frozenset({ErrorCode.CSRF, ErrorCode.AUTH_EXPIRED, ErrorCode.AUTH_REQUIRED})


class OfferSubmission(ReadBackLookups):
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
        try:
            return await self._submit_offer_steps(
                request,
                before_remote_attempt=before_remote_attempt,
            )
        except GatewayError as error:
            if error.code in _WEB_SESSION_ERRORS:
                # The kwork.ru cookie session can expire while the API token
                # stays valid; force a fresh web login on the next attempt.
                self.session.invalidate_web_login()
            raise

    async def _submit_offer_steps(
        self,
        request: dict[str, Any],
        *,
        before_remote_attempt: Callable[[], Awaitable[None]] | None,
    ) -> dict[str, JsonValue]:
        client = await self.session.ensure_web_client()
        project_id = int(request["project_id"])
        referer = urljoin(client.web.base_url, f"new_offer?project={project_id}")
        # Opening the form, FAQ init, the draft and the template check cannot
        # create an offer, so a failure there leaves the write committable.
        # Only the final create call crosses the durable no-retry boundary.
        page = await self.session.call_write_step(
            "write-offer-page",
            lambda current: current.web.open_new_offer_page(project_id=project_id),
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
        )
        self._require_web_prerequisite(draft_result, "wants/create_offer_draft")
        template_result = await self.session.call_write_step(
            "write-offer-template-check",
            lambda current: current.web.check_is_template(
                want_id=project_id,
                description=request["description"],
                referer=referer,
            ),
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
            # Kwork confirmed the offer exists; any failure to find its ID is
            # an unknown outcome, never a known failure that is safe to retry.
            try:
                matches = await self._matching_offers(request)
            except AmbiguousWriteError:
                raise
            except GatewayError as error:
                raise AmbiguousWriteError("offer_created_readback_failed") from error
            if len(matches) == 1:
                offer_id = matches[0].offer_id
            else:
                raise AmbiguousWriteError("offer_success_without_confirmed_id")
        return {
            "offer_id": offer_id,
            "project_id": project_id,
        }
