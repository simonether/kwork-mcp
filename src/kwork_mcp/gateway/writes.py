"""Durable prepare → commit → reconcile protocol over the shared write ledger."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, cast

from loguru import logger
from pydantic import JsonValue, TypeAdapter, ValidationError

from kwork_mcp.coordination import StoredWrite
from kwork_mcp.errors import AmbiguousWriteError, ContractDriftError, GatewayError
from kwork_mcp.gateway.actions import ACTION_HANDLERS
from kwork_mcp.gateway.offer_flow import OfferSubmission
from kwork_mcp.gateway.parsing import _finish_shielded_task, _positive_int
from kwork_mcp.models import (
    ErrorCode,
    ErrorInfo,
    WriteAction,
    WriteRequest,
    WriteState,
    WriteStatusData,
)
from kwork_mcp.security import sanitize_external

_WRITE_REQUEST_ADAPTER: TypeAdapter[WriteRequest] = TypeAdapter(WriteRequest)

# Preflight findings that settle the write for good. Any other preflight
# failure means the checks could not conclude, so the write stays committable.
_DEFINITIVE_PREFLIGHT_CODES = frozenset(
    {
        ErrorCode.NOT_FOUND,
        ErrorCode.CLOSED_PROJECT,
        ErrorCode.DUPLICATE,
        ErrorCode.INSUFFICIENT_CONNECTS,
        ErrorCode.VALIDATION,
        ErrorCode.PERMISSION,
    }
)


def _preflight_failure(error: GatewayError) -> GatewayError:
    """Read-only checks that cannot conclude are retryable, never reconcilable."""

    if error.code is not ErrorCode.AMBIGUOUS_WRITE:
        return error
    return GatewayError(
        ErrorCode.UPSTREAM_UNAVAILABLE,
        retryable=True,
        safe_to_retry=True,
        diagnostic=f"preflight_inconclusive:{error.diagnostic}",
    )


class _InconclusiveCommitPreflightError(Exception):
    def __init__(self, error: GatewayError) -> None:
        super().__init__(error.safe_message)
        self.error = error


class WriteProtocol(OfferSubmission):
    async def _preflight(
        self,
        request: WriteRequest,
        resolved: dict[str, JsonValue],
    ) -> None:
        handler = ACTION_HANDLERS.get(request.action)
        if handler is not None:
            await handler.preflight(self, request, resolved)

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
        try:
            await self._preflight(request, resolved)
        except GatewayError as error:
            raise _preflight_failure(error) from error
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
            try:
                await self._preflight(request_model, fresh_resolved)
            except GatewayError as error:
                if error.code in _DEFINITIVE_PREFLIGHT_CODES:
                    raise
                raise _InconclusiveCommitPreflightError(_preflight_failure(error)) from error
            handler = ACTION_HANDLERS.get(claimed.action)
            if handler is not None:
                handler.check_fresh_resolution(stored_resolved, fresh_resolved)
        except asyncio.CancelledError:
            await self._settle_cancelled_claim_safely(
                write_id=write_id,
                scope=scope,
                correlation_id=correlation_id,
                remote_started=False,
            )
            raise
        except _InconclusiveCommitPreflightError as inconclusive:
            released = await self._await_durable_ledger(
                self.coordinator.release_write_claim(
                    write_id=write_id,
                    scope=scope,
                    owner=self.instance_id,
                    note="inconclusive_commit_preflight",
                )
            )
            if released.state is not WriteState.PREPARED:
                return self._write_status(released, correlation_id=correlation_id)
            raise inconclusive.error from inconclusive.__cause__
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
                # Nothing reached Kwork, so nothing is ambiguous: report the
                # underlying failure (e.g. a local throttle) as retryable.
                cause = error.__cause__
                raise (cause if isinstance(cause, GatewayError) else _preflight_failure(error)) from error
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
        handler = ACTION_HANDLERS.get(record.action)
        if handler is None:
            raise ContractDriftError(f"unimplemented_write_action:{record.action}")
        return await handler.execute(
            self,
            request,
            resolved,
            before_remote_attempt=before_remote_attempt,
        )

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
        handler = ACTION_HANDLERS.get(action)
        if handler is None:
            raise ContractDriftError(f"reconciliation_not_implemented:{action}")
        return await handler.read_back(self, request, resolved, record)
