from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from kwork_mcp.models import (
    ErrorCode,
    ErrorInfo,
    ResultEnvelope,
    WriteAction,
    WriteState,
    WriteStatusData,
)
from kwork_mcp.tools.common import write_result

WriteOutcome = ResultEnvelope[WriteStatusData]


def _status(
    state: WriteState,
    *,
    terminal_error: ErrorInfo | None = None,
) -> WriteStatusData:
    now = datetime.now(UTC)
    return WriteStatusData(
        write_id="00000000-0000-4000-8000-000000000001",
        idempotency_key="common-branch-test",
        action=WriteAction.MARK_DIALOG_READ,
        state=state,
        payload_hash="a" * 64,
        payload={"request": {"action": "mark_dialog_read", "user_id": 42}},
        prepared_at=now,
        expires_at=now + timedelta(minutes=10),
        updated_at=now,
        can_commit=state is WriteState.PREPARED,
        reconciliation_required=state is WriteState.SUBMISSION_UNKNOWN,
        terminal_error=terminal_error,
    )


@pytest.mark.parametrize(
    ("status", "error_code"),
    [
        (
            _status(
                WriteState.FAILED_KNOWN,
                terminal_error=ErrorInfo(
                    code=ErrorCode.PERMISSION,
                    message="safe",
                    retryable=False,
                    safe_to_retry=False,
                    reconciliation_required=False,
                    correlation_id="stored",
                ),
            ),
            ErrorCode.PERMISSION,
        ),
        (_status(WriteState.FAILED_KNOWN), ErrorCode.INTERNAL),
        (_status(WriteState.EXPIRED), ErrorCode.PREPARATION_EXPIRED),
    ],
)
def test_write_result_preserves_terminal_failure_taxonomy(
    status: WriteStatusData,
    error_code: ErrorCode,
) -> None:
    result = write_result(
        WriteOutcome,
        status=status,
        correlation="current",
    )
    assert result.is_error is True
    assert result.structured_content is not None
    assert result.structured_content["error"]["code"] == error_code.value
    assert result.structured_content["data"]["state"] == status.state.value


def test_write_result_marks_reconciled_absence_as_known_empty() -> None:
    result = write_result(
        WriteOutcome,
        status=_status(WriteState.RECONCILED_ABSENT),
        correlation="current",
    )
    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["knowledge_state"] == "known_empty"


def test_status_query_reports_unknown_state_without_retrying_it() -> None:
    result = write_result(
        WriteOutcome,
        status=_status(WriteState.SUBMISSION_UNKNOWN),
        correlation="current",
        status_query=True,
    )
    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["knowledge_state"] == "known_data"
    assert result.structured_content["data"]["reconciliation_required"] is True
