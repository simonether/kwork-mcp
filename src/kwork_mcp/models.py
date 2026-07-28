"""Stable wire models for the MCP interface.

Every tool returns a :class:`ResultEnvelope`.  Kwork data is deliberately kept in
``raw`` fields as well as normalized stable identifiers so upstream fields are not
silently discarded.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    model_validator,
)


class KnowledgeState(StrEnum):
    KNOWN_DATA = "known_data"
    KNOWN_EMPTY = "known_empty"
    UNKNOWN_ERROR = "unknown_error"


class ErrorCode(StrEnum):
    AUTH_REQUIRED = "auth_required"
    AUTH_EXPIRED = "auth_expired"
    AUTH_IN_PROGRESS = "auth_in_progress"
    ACCOUNT_BINDING_REQUIRED = "account_binding_required"
    ACCOUNT_MISMATCH = "account_mismatch"
    CAPTCHA = "captcha"
    PERMISSION = "permission"
    IP_BLOCKED = "ip_blocked"
    CSRF = "csrf"
    RATE_LIMIT = "rate_limit"
    CIRCUIT_OPEN = "circuit_open"
    PROXY = "proxy"
    TIMEOUT = "timeout"
    CLOSED_PROJECT = "closed_project"
    DUPLICATE = "duplicate"
    INSUFFICIENT_CONNECTS = "insufficient_connects"
    CONTRACT_DRIFT = "contract_drift"
    CREDENTIAL_UPDATE_UNKNOWN = "credential_update_unknown"
    AMBIGUOUS_WRITE = "ambiguous_write"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    PREPARATION_EXPIRED = "preparation_expired"
    INVALID_CONFIRMATION = "invalid_confirmation"
    WRITE_IN_PROGRESS = "write_in_progress"
    WRITE_DISABLED = "write_disabled"
    NOT_FOUND = "not_found"
    VALIDATION = "validation"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    INTERNAL = "internal"


class ErrorInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str
    retryable: bool = False
    safe_to_retry: bool = False
    reconciliation_required: bool = False
    retry_after_seconds: float | None = None
    correlation_id: str


class ResultMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["kwork"] = "kwork"
    content_trust: Literal["external_untrusted"] = "external_untrusted"
    observed_at: datetime
    correlation_id: str
    upstream_contract: Literal["kwork==0.2.0"] = "kwork==0.2.0"


class ResultEnvelope[DataT](BaseModel):
    """One schema for success, empty success, and typed tool errors."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    knowledge_state: KnowledgeState
    summary: str
    data: DataT | None = None
    error: ErrorInfo | None = None
    meta: ResultMeta

    @model_validator(mode="after")
    def validate_state(self) -> ResultEnvelope[DataT]:
        if self.knowledge_state is KnowledgeState.UNKNOWN_ERROR and self.error is None:
            raise ValueError("unknown_error requires error")
        if self.knowledge_state is not KnowledgeState.UNKNOWN_ERROR and self.error is not None:
            raise ValueError("successful knowledge state cannot contain error")
        if self.knowledge_state is KnowledgeState.KNOWN_DATA and self.data is None:
            raise ValueError("known_data requires data")
        return self


class AccountData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: int
    username: str
    expected_user_id: int | None
    expected_username: str | None
    binding_state: Literal["bound", "unbound_reads_only"]
    writes_enabled: bool
    write_ready: bool
    raw: dict[str, JsonValue]


class ConnectsData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active: int
    total: int
    raw: dict[str, JsonValue]


class UserRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: int
    username: str | None = None
    raw: dict[str, JsonValue]


class PageInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int
    page_size: int | None = None
    total_items: int | None = None
    total_pages: int | None = None
    has_more: bool | None = None
    next_cursor: str | None = None
    query_fingerprint: str | None = None
    high_watermark: str | None = None


class ItemCollection[ItemT](BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ItemT]
    page: PageInfo | None = None
    completeness: Literal["complete", "partial_upstream_limit"] = "complete"
    raw_metadata: dict[str, JsonValue] = Field(default_factory=dict)


class ProjectRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: int
    title: str | None = None
    description: str | None = None
    status: str | int | None = None
    customer_id: int | None = None
    customer_username: str | None = None
    price: int | float | None = None
    possible_price_limit: int | float | None = None
    offers_count: int | None = None
    category_id: int | None = None
    published_at: int | str | None = None
    raw: dict[str, JsonValue]


class ProjectDiscoveryData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["favorites", "all", "category_ids"]
    category_ids: list[int]
    projects: ItemCollection[ProjectRecord]
    connects: dict[str, JsonValue] | None = None


class OfferRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    offer_id: int
    project_id: int
    title: str | None = None
    description: str | None = None
    status: str | int | None = None
    price: int | float | None = None
    duration_days: int | None = None
    created_at: int | str | None = None
    raw: dict[str, JsonValue]


class OrderRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: int
    status: int | str | None = None
    title: str | None = None
    buyer_id: int | None = None
    buyer_username: str | None = None
    raw: dict[str, JsonValue]


class DialogRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: int | None = None
    username: str
    unread_count: int | None = None
    raw: dict[str, JsonValue]


class MessageRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: int | None = None
    sender_id: int | None = None
    sender_username: str | None = None
    text: str | None = None
    created_at: int | str | None = None
    raw: dict[str, JsonValue]


class KworkRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kwork_id: int
    title: str | None = None
    status_group_id: int | None = None
    status_group_name: str | None = None
    raw: dict[str, JsonValue]


class CategoryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category_id: int
    name: str | None = None
    children: list[CategoryRecord] = Field(default_factory=list)
    raw: dict[str, JsonValue]


class RawObjectData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    raw: dict[str, JsonValue] | list[JsonValue]


class WriteAction(StrEnum):
    SUBMIT_OFFER = "submit_offer"
    DELETE_OFFER = "delete_offer"
    SEND_MESSAGE = "send_message"
    EDIT_MESSAGE = "edit_message"
    DELETE_MESSAGE = "delete_message"
    MARK_DIALOG_READ = "mark_dialog_read"
    SUBMIT_ORDER_APPROVAL = "submit_order_approval"
    SET_KWORK_STATE = "set_kwork_state"


class SubmitOfferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal[WriteAction.SUBMIT_OFFER]
    project_id: Annotated[int, Field(gt=0)]
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    description: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=150, max_length=10_000),
    ]
    price: Annotated[int, Field(gt=0, le=10_000_000)]
    duration_days: Annotated[int, Field(gt=0, le=365)]


class DeleteOfferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal[WriteAction.DELETE_OFFER]
    offer_id: Annotated[int, Field(gt=0)]


class SendMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal[WriteAction.SEND_MESSAGE]
    user_id: Annotated[int, Field(gt=0)] | None = None
    username: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)] | None = None
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10_000)]

    @model_validator(mode="after")
    def exactly_one_recipient(self) -> SendMessageRequest:
        if (self.user_id is None) == (self.username is None):
            raise ValueError("provide exactly one of user_id or username")
        return self


class EditMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal[WriteAction.EDIT_MESSAGE]
    message_id: Annotated[int, Field(gt=0)]
    username: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10_000)]


class DeleteMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal[WriteAction.DELETE_MESSAGE]
    message_id: Annotated[int, Field(gt=0)]
    username: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]


class MarkDialogReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal[WriteAction.MARK_DIALOG_READ]
    user_id: Annotated[int, Field(gt=0)]


class SubmitOrderApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal[WriteAction.SUBMIT_ORDER_APPROVAL]
    order_id: Annotated[int, Field(gt=0)]
    metrics: list[Annotated[int, Field(ge=0)]] = Field(default_factory=list, max_length=100)
    stage_ids: list[Annotated[int, Field(gt=0)]] = Field(default_factory=list, max_length=100)
    file_ids: list[Annotated[int, Field(gt=0)]] = Field(default_factory=list, max_length=100)


class SetKworkStateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal[WriteAction.SET_KWORK_STATE]
    kwork_id: Annotated[int, Field(gt=0)]
    target_state: Literal["active", "paused"]


WriteRequest = Annotated[
    SubmitOfferRequest
    | DeleteOfferRequest
    | SendMessageRequest
    | EditMessageRequest
    | DeleteMessageRequest
    | MarkDialogReadRequest
    | SubmitOrderApprovalRequest
    | SetKworkStateRequest,
    Field(discriminator="action"),
]

IdempotencyKey = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]


class WriteState(StrEnum):
    PREPARED = "prepared"
    COMMITTING = "committing"
    SUCCEEDED = "succeeded"
    FAILED_KNOWN = "failed_known"
    SUBMISSION_UNKNOWN = "submission_unknown"
    RECONCILED_SUCCEEDED = "reconciled_succeeded"
    RECONCILED_ABSENT = "reconciled_absent"
    EXPIRED = "expired"


class WriteStatusData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    write_id: str
    idempotency_key: str
    action: WriteAction
    state: WriteState
    payload_hash: str
    payload: dict[str, JsonValue]
    prepared_at: datetime
    expires_at: datetime
    updated_at: datetime
    can_commit: bool
    reconciliation_required: bool
    confirmation_token: str | None = None
    result: dict[str, JsonValue] | None = None
    terminal_error: ErrorInfo | None = None


class ContractStatusData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    distribution: Literal["kwork"]
    version: Literal["0.2.0"]
    signature_fingerprint: str
    verified: bool
