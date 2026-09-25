"""Per-action write rules: preflight, the remote call, and read-back evidence.

Each supported ``WriteAction`` has exactly one handler, so everything that
decides whether an action may run, how it is sent, and how its outcome is
proven lives in one place instead of three parallel ``if/elif`` chains.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, ClassVar, cast

from pydantic import BaseModel, JsonValue

from kwork_mcp.contracts import enforce_route_params
from kwork_mcp.coordination import StoredWrite
from kwork_mcp.errors import AmbiguousWriteError, ContractDriftError, GatewayError
from kwork_mcp.gateway.offer_flow import OfferSubmission
from kwork_mcp.gateway.parsing import (
    _as_json_dict,
    _normalize_remote_text,
    _optional_int,
    _positive_int,
    _response,
)
from kwork_mcp.models import (
    DeleteMessageRequest,
    DeleteOfferRequest,
    EditMessageRequest,
    ErrorCode,
    MarkDialogReadRequest,
    SendMessageRequest,
    SetKworkStateRequest,
    SubmitOfferRequest,
    SubmitOrderApprovalRequest,
    WriteAction,
)

BeforeRemoteAttempt = Callable[[], Awaitable[None]] | None
ReadBack = tuple[bool, dict[str, JsonValue]]


class ActionHandler[RequestT: BaseModel]:
    """Rules for one write action.

    ``preflight`` runs read-only checks at prepare and again at commit time and
    records facts in ``resolved``. ``execute`` performs the single remote write.
    ``read_back`` returns ``(True, result)`` when the side effect is proven,
    ``(False, result)`` when its absence is proven, and raises
    ``AmbiguousWriteError`` when the evidence is inconclusive.
    """

    action: ClassVar[WriteAction]

    async def preflight(
        self,
        gateway: OfferSubmission,
        request: RequestT,
        resolved: dict[str, JsonValue],
    ) -> None:
        return None

    def check_fresh_resolution(
        self,
        stored: dict[str, Any],
        fresh: dict[str, JsonValue],
    ) -> None:
        """Reject a commit when commit-time preflight resolved different facts."""

    async def execute(
        self,
        gateway: OfferSubmission,
        request: dict[str, Any],
        resolved: dict[str, Any],
        *,
        before_remote_attempt: BeforeRemoteAttempt,
    ) -> dict[str, JsonValue]:
        raise NotImplementedError

    async def read_back(
        self,
        gateway: OfferSubmission,
        request: dict[str, Any],
        resolved: dict[str, Any],
        record: StoredWrite,
    ) -> ReadBack:
        raise NotImplementedError


class SubmitOfferHandler(ActionHandler[SubmitOfferRequest]):
    action = WriteAction.SUBMIT_OFFER

    async def preflight(
        self,
        gateway: OfferSubmission,
        request: SubmitOfferRequest,
        resolved: dict[str, JsonValue],
    ) -> None:
        project = await gateway.get_project(request.project_id)
        if project is None:
            raise GatewayError(ErrorCode.NOT_FOUND, diagnostic="project_not_found")
        status = str(project.status or "").casefold()
        if any(value in status for value in ("closed", "archived", "stopped", "закры")):
            raise GatewayError(ErrorCode.CLOSED_PROJECT, diagnostic=f"status={status}")
        if any(offer.project_id == request.project_id for offer in await gateway._all_offers()):
            raise GatewayError(ErrorCode.DUPLICATE, diagnostic="offer_exists_for_project")
        connects = await gateway.get_connects()
        if connects.active <= 0:
            raise GatewayError(
                ErrorCode.INSUFFICIENT_CONNECTS,
                diagnostic="active_connects=0",
            )
        resolved["project_status_at_prepare"] = project.status
        resolved["connects_at_prepare"] = connects.active

    async def execute(
        self,
        gateway: OfferSubmission,
        request: dict[str, Any],
        resolved: dict[str, Any],
        *,
        before_remote_attempt: BeforeRemoteAttempt,
    ) -> dict[str, JsonValue]:
        return await gateway._execute_submit_offer(
            request,
            before_remote_attempt=before_remote_attempt,
        )

    async def read_back(
        self,
        gateway: OfferSubmission,
        request: dict[str, Any],
        resolved: dict[str, Any],
        record: StoredWrite,
    ) -> ReadBack:
        project_id = int(request["project_id"])
        matches = await gateway._matching_offers(request, record=record)
        if len(matches) > 1:
            raise AmbiguousWriteError("multiple_matching_offers")
        if matches:
            return True, {
                "offer_id": matches[0].offer_id,
                "project_id": project_id,
                "reconciled": True,
            }
        return False, {"project_id": project_id, "offer_absent": True}


class DeleteOfferHandler(ActionHandler[DeleteOfferRequest]):
    action = WriteAction.DELETE_OFFER

    async def preflight(
        self,
        gateway: OfferSubmission,
        request: DeleteOfferRequest,
        resolved: dict[str, JsonValue],
    ) -> None:
        if await gateway.get_offer(request.offer_id) is None:
            raise GatewayError(ErrorCode.NOT_FOUND, diagnostic="offer_not_found")

    async def execute(
        self,
        gateway: OfferSubmission,
        request: dict[str, Any],
        resolved: dict[str, Any],
        *,
        before_remote_attempt: BeforeRemoteAttempt,
    ) -> dict[str, JsonValue]:
        delete_offer_params = {"id": request["offer_id"]}
        enforce_route_params("delete_offer", delete_offer_params)
        data = await gateway.session.call_write_step(
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

    async def read_back(
        self,
        gateway: OfferSubmission,
        request: dict[str, Any],
        resolved: dict[str, Any],
        record: StoredWrite,
    ) -> ReadBack:
        offer = await gateway.get_offer(int(request["offer_id"]))
        return (
            offer is None,
            {
                "offer_id": request["offer_id"],
                "deleted": offer is None,
                "reconciled": True,
            },
        )


class SendMessageHandler(ActionHandler[SendMessageRequest]):
    action = WriteAction.SEND_MESSAGE

    async def preflight(
        self,
        gateway: OfferSubmission,
        request: SendMessageRequest,
        resolved: dict[str, JsonValue],
    ) -> None:
        if request.user_id is not None:
            resolved["user_id"] = request.user_id
        else:
            resolved["user_id"] = await gateway._resolve_user_id(cast(str, request.username))

    def check_fresh_resolution(
        self,
        stored: dict[str, Any],
        fresh: dict[str, JsonValue],
    ) -> None:
        if fresh.get("user_id") != stored.get("user_id"):
            raise GatewayError(
                ErrorCode.CONTRACT_DRIFT,
                diagnostic="recipient_identity_changed_since_prepare",
            )

    async def execute(
        self,
        gateway: OfferSubmission,
        request: dict[str, Any],
        resolved: dict[str, Any],
        *,
        before_remote_attempt: BeforeRemoteAttempt,
    ) -> dict[str, JsonValue]:
        user_id = _positive_int(resolved.get("user_id"))
        if user_id is None:
            raise ContractDriftError("send_message:missing_resolved_user_id")
        data = await gateway.session.call_write_step(
            "write-send-message",
            lambda client: client.send_message(user_id=user_id, text=request["text"]),
            before_remote_attempt=before_remote_attempt,
        )
        raw_response = _response(data, "inboxCreate")
        raw = _as_json_dict(
            raw_response,
            "inboxCreate.response",
            secrets_to_redact=gateway._redaction_secrets,
        )
        message_id = _positive_int(raw.get("id"))
        if message_id is None:
            raise AmbiguousWriteError("inboxCreate_missing_message_id")
        return {"message_id": message_id, "user_id": user_id}

    async def read_back(
        self,
        gateway: OfferSubmission,
        request: dict[str, Any],
        resolved: dict[str, Any],
        record: StoredWrite,
    ) -> ReadBack:
        user_id = _positive_int(resolved.get("user_id"))
        if user_id is None:
            raise ContractDriftError("reconcile_message_missing_user_id")
        username = request.get("username")
        if not isinstance(username, str):
            user = await gateway.get_user(user_id=user_id, username=None)
            username = user.username if user is not None else None
        if not username:
            raise ContractDriftError("reconcile_message_missing_username")
        sender_id = _positive_int(gateway.config.expected_user_id)
        if sender_id is None:
            raise ContractDriftError("reconcile_message_missing_sender_binding")
        message_matches = await gateway._matching_sent_messages(
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


async def _require_existing_message(
    gateway: OfferSubmission,
    request: EditMessageRequest | DeleteMessageRequest,
    resolved: dict[str, JsonValue],
) -> None:
    message = await gateway._find_message(
        username=request.username,
        message_id=request.message_id,
    )
    if message is None:
        raise GatewayError(
            ErrorCode.NOT_FOUND,
            diagnostic="message_not_found_in_expected_dialog",
        )
    resolved["message_sender_id_at_prepare"] = message.sender_id


class EditMessageHandler(ActionHandler[EditMessageRequest]):
    action = WriteAction.EDIT_MESSAGE

    async def preflight(
        self,
        gateway: OfferSubmission,
        request: EditMessageRequest,
        resolved: dict[str, JsonValue],
    ) -> None:
        await _require_existing_message(gateway, request, resolved)

    async def execute(
        self,
        gateway: OfferSubmission,
        request: dict[str, Any],
        resolved: dict[str, Any],
        *,
        before_remote_attempt: BeforeRemoteAttempt,
    ) -> dict[str, JsonValue]:
        edit_message_params = {"id": request["message_id"]}
        enforce_route_params("inbox_edit_query", edit_message_params)
        data = await gateway.session.call_write_step(
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

    async def read_back(
        self,
        gateway: OfferSubmission,
        request: dict[str, Any],
        resolved: dict[str, Any],
        record: StoredWrite,
    ) -> ReadBack:
        message = await gateway._find_message(
            username=str(request["username"]),
            message_id=int(request["message_id"]),
        )
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


class DeleteMessageHandler(ActionHandler[DeleteMessageRequest]):
    action = WriteAction.DELETE_MESSAGE

    async def preflight(
        self,
        gateway: OfferSubmission,
        request: DeleteMessageRequest,
        resolved: dict[str, JsonValue],
    ) -> None:
        await _require_existing_message(gateway, request, resolved)

    async def execute(
        self,
        gateway: OfferSubmission,
        request: dict[str, Any],
        resolved: dict[str, Any],
        *,
        before_remote_attempt: BeforeRemoteAttempt,
    ) -> dict[str, JsonValue]:
        data = await gateway.session.call_write_step(
            "write-delete-message",
            lambda client: client.delete_message(request["message_id"]),
            before_remote_attempt=before_remote_attempt,
        )
        _response(data, "inboxDelete", require_response=False)
        return {"message_id": request["message_id"], "deleted": True}

    async def read_back(
        self,
        gateway: OfferSubmission,
        request: dict[str, Any],
        resolved: dict[str, Any],
        record: StoredWrite,
    ) -> ReadBack:
        message = await gateway._find_message(
            username=str(request["username"]),
            message_id=int(request["message_id"]),
        )
        success = message is None
        return success, {
            "message_id": request["message_id"],
            "reconciled": True,
            "desired_state_present": success,
        }


class MarkDialogReadHandler(ActionHandler[MarkDialogReadRequest]):
    action = WriteAction.MARK_DIALOG_READ

    async def preflight(
        self,
        gateway: OfferSubmission,
        request: MarkDialogReadRequest,
        resolved: dict[str, JsonValue],
    ) -> None:
        dialog = await gateway._find_dialog_by_user_id(request.user_id)
        if dialog is None:
            raise GatewayError(ErrorCode.NOT_FOUND, diagnostic="dialog_not_found")
        resolved["dialog_username_at_prepare"] = dialog.username

    async def execute(
        self,
        gateway: OfferSubmission,
        request: dict[str, Any],
        resolved: dict[str, Any],
        *,
        before_remote_attempt: BeforeRemoteAttempt,
    ) -> dict[str, JsonValue]:
        mark_read_params = {"user_id": request["user_id"]}
        enforce_route_params("inbox_read", mark_read_params)
        data = await gateway.session.call_write_step(
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

    async def read_back(
        self,
        gateway: OfferSubmission,
        request: dict[str, Any],
        resolved: dict[str, Any],
        record: StoredWrite,
    ) -> ReadBack:
        dialog_record = await gateway._find_dialog_by_user_id(int(request["user_id"]))
        if dialog_record is not None and dialog_record.unread_count is None:
            raise AmbiguousWriteError("dialog_unread_count_missing")
        success = dialog_record is not None and dialog_record.unread_count == 0
        return success, {
            "user_id": request["user_id"],
            "read": success,
            "reconciled": True,
        }


class SubmitOrderApprovalHandler(ActionHandler[SubmitOrderApprovalRequest]):
    action = WriteAction.SUBMIT_ORDER_APPROVAL

    async def preflight(
        self,
        gateway: OfferSubmission,
        request: SubmitOrderApprovalRequest,
        resolved: dict[str, JsonValue],
    ) -> None:
        orders = await gateway._all_orders()
        order = next((item for item in orders if item.order_id == request.order_id), None)
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

    async def execute(
        self,
        gateway: OfferSubmission,
        request: dict[str, Any],
        resolved: dict[str, Any],
        *,
        before_remote_attempt: BeforeRemoteAttempt,
    ) -> dict[str, JsonValue]:
        approval_params: dict[str, Any] = {"orderId": request["order_id"]}
        if request.get("metrics"):
            approval_params["metrics[]"] = request["metrics"]
        if request.get("stage_ids"):
            approval_params["stageIds[]"] = request["stage_ids"]
        if request.get("file_ids"):
            approval_params["filesIds[]"] = request["file_ids"]
        enforce_route_params("send_order_for_approval", approval_params)
        data = await gateway.session.call_write_step(
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

    async def read_back(
        self,
        gateway: OfferSubmission,
        request: dict[str, Any],
        resolved: dict[str, Any],
        record: StoredWrite,
    ) -> ReadBack:
        orders = await gateway._all_orders()
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


class SetKworkStateHandler(ActionHandler[SetKworkStateRequest]):
    action = WriteAction.SET_KWORK_STATE

    async def preflight(
        self,
        gateway: OfferSubmission,
        request: SetKworkStateRequest,
        resolved: dict[str, JsonValue],
    ) -> None:
        records = await gateway.list_my_kworks()
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

    async def execute(
        self,
        gateway: OfferSubmission,
        request: dict[str, Any],
        resolved: dict[str, Any],
        *,
        before_remote_attempt: BeforeRemoteAttempt,
    ) -> dict[str, JsonValue]:
        kwork_state_params = {"kwork_id": request["kwork_id"]}
        route = "start_kwork" if request["target_state"] == "active" else "pause_kwork"
        enforce_route_params(route, kwork_state_params)
        if route == "start_kwork":
            data = await gateway.session.call_write_step(
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
            data = await gateway.session.call_write_step(
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

    async def read_back(
        self,
        gateway: OfferSubmission,
        request: dict[str, Any],
        resolved: dict[str, Any],
        record: StoredWrite,
    ) -> ReadBack:
        kworks = await gateway.list_my_kworks()
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


ACTION_HANDLERS: dict[WriteAction, ActionHandler[Any]] = {
    handler.action: handler
    for handler in (
        SubmitOfferHandler(),
        DeleteOfferHandler(),
        SendMessageHandler(),
        EditMessageHandler(),
        DeleteMessageHandler(),
        MarkDialogReadHandler(),
        SubmitOrderApprovalHandler(),
        SetKworkStateHandler(),
    )
}
