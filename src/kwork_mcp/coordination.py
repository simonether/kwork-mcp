"""Shared SQLite coordination for rate limits, circuits, and durable writes."""

from __future__ import annotations

import asyncio
import base64
import fcntl
import hashlib
import hmac
import json
import math
import os
import random
import secrets
import sqlite3
import stat
import time
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from kwork_mcp.config import KworkConfig
from kwork_mcp.errors import AmbiguousWriteError, ContractDriftError, GatewayError
from kwork_mcp.models import ErrorCode, ErrorInfo, WriteAction, WriteState
from kwork_mcp.security import cancellation_safe_fd_guard, ensure_secure_directory

ResultT = TypeVar("ResultT")


@dataclass(frozen=True, slots=True)
class StoredWrite:
    write_id: str
    scope: str
    idempotency_key: str
    action: WriteAction
    payload_json: str
    payload_hash: str
    state: WriteState
    prepared_at: float
    expires_at: float
    updated_at: float
    lease_owner: str | None
    lease_expires: float | None
    remote_started_at: float | None
    result_json: str | None
    error_json: str | None


@dataclass(frozen=True, slots=True)
class PreparedRecord:
    record: StoredWrite
    confirmation_token: str | None


class CoordinationStore:
    """A short-transaction shared state database.

    Network calls and sleeps never run while a SQLite transaction is held.
    """

    SCHEMA_VERSION = 3

    def __init__(self, config: KworkConfig) -> None:
        self._config = config
        self._instance_id = str(uuid.uuid4())
        self._policy_fingerprint = self._shared_policy_fingerprint(config)
        self._state_dir = ensure_secure_directory(config.state_dir)
        self._path = self._state_dir / "coordination.sqlite3"
        self._initialize()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def policy_fingerprint(self) -> str:
        return self._policy_fingerprint

    @staticmethod
    def _shared_policy_fingerprint(config: KworkConfig) -> str:
        policy = {
            "version": 1,
            "rps_limit": config.rps_limit,
            "burst_limit": config.burst_limit,
            "route_rps_limit": config.route_rps_limit,
            "route_burst_limit": config.route_burst_limit,
            "timeout": config.timeout,
            "circuit_failure_threshold": config.circuit_failure_threshold,
            "circuit_open_seconds": config.circuit_open_seconds,
            "preparation_ttl_seconds": config.preparation_ttl_seconds,
            "write_lease_seconds": config.write_lease_seconds,
            "reconciliation_min_age_seconds": config.reconciliation_min_age_seconds,
            "reconciliation_absence_confirmations": config.reconciliation_absence_confirmations,
            "reconciliation_absence_interval_seconds": config.reconciliation_absence_interval_seconds,
            "state_retention_days": config.state_retention_days,
        }
        encoded = json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _initialize(self) -> None:
        if not self._path.exists():
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                fd = os.open(self._path, flags, 0o600)
            except FileExistsError:
                pass
            else:
                try:
                    os.fchmod(fd, 0o600)
                finally:
                    os.close(fd)
        info = self._path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise GatewayError(ErrorCode.VALIDATION, diagnostic="coordination_db_not_regular")
        if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
            raise GatewayError(ErrorCode.VALIDATION, diagnostic="coordination_db_wrong_owner")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise GatewayError(ErrorCode.VALIDATION, diagnostic="coordination_db_permissions")

        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rate_buckets (
                    bucket_key TEXT PRIMARY KEY,
                    tokens REAL NOT NULL,
                    last_refill REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS circuits (
                    scope TEXT NOT NULL,
                    route TEXT NOT NULL,
                    failures INTEGER NOT NULL,
                    open_until REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    probe_owner TEXT,
                    probe_until REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (scope, route)
                );
                CREATE TABLE IF NOT EXISTS writes (
                    write_id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    action TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    confirmation_hash TEXT NOT NULL,
                    state TEXT NOT NULL,
                    prepared_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    lease_owner TEXT,
                    lease_expires REAL,
                    remote_started_at REAL,
                    result_json TEXT,
                    error_json TEXT,
                    UNIQUE (scope, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS writes_state_idx
                    ON writes(scope, state, updated_at);
                CREATE UNIQUE INDEX IF NOT EXISTS writes_one_committer_per_scope_idx
                    ON writes(scope) WHERE state = 'committing';
                CREATE TABLE IF NOT EXISTS write_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    write_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    occurred_at REAL NOT NULL,
                    note TEXT,
                    FOREIGN KEY(write_id) REFERENCES writes(write_id)
                );
                """
            )
            current = conn.execute("PRAGMA user_version").fetchone()[0]
            if current not in {0, 1, 2, self.SCHEMA_VERSION}:
                raise GatewayError(
                    ErrorCode.CONTRACT_DRIFT,
                    diagnostic=f"coordination_schema={current}",
                )
            circuit_columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(circuits)").fetchall()}
            if "probe_owner" not in circuit_columns:
                conn.execute("ALTER TABLE circuits ADD COLUMN probe_owner TEXT")
            if "probe_until" not in circuit_columns:
                conn.execute("ALTER TABLE circuits ADD COLUMN probe_until REAL NOT NULL DEFAULT 0")
            write_columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(writes)").fetchall()}
            if "remote_started_at" not in write_columns:
                conn.execute("ALTER TABLE writes ADD COLUMN remote_started_at REAL")
            if current < self.SCHEMA_VERSION:
                # Schemas before v3 could not distinguish a pre-remote claim
                # from an upstream request that had already started.  Treat
                # every inherited committing row as ambiguous; resetting it to
                # prepared would permit a duplicate remote side effect.
                migrated_at = time.time()
                legacy_committing = conn.execute(
                    "SELECT write_id,updated_at FROM writes WHERE state=?",
                    (WriteState.COMMITTING.value,),
                ).fetchall()
                if legacy_committing:
                    migration_error = json.dumps(
                        {
                            "code": ErrorCode.AMBIGUOUS_WRITE.value,
                            "message": "legacy writer state requires reconciliation",
                        },
                        sort_keys=True,
                    )
                    conn.execute(
                        """
                        UPDATE writes SET state=?,updated_at=?,lease_owner=NULL,
                            lease_expires=NULL,
                            remote_started_at=COALESCE(remote_started_at,updated_at),
                            error_json=?
                        WHERE state=?
                        """,
                        (
                            WriteState.SUBMISSION_UNKNOWN.value,
                            migrated_at,
                            migration_error,
                            WriteState.COMMITTING.value,
                        ),
                    )
                    conn.executemany(
                        """
                        INSERT INTO write_events(write_id,state,occurred_at,note)
                        VALUES(?,?,?,?)
                        """,
                        [
                            (
                                str(row["write_id"]),
                                WriteState.SUBMISSION_UNKNOWN.value,
                                migrated_at,
                                "v3_conservative_remote_boundary_migration",
                            )
                            for row in legacy_committing
                        ],
                    )
            conn.execute(f"PRAGMA user_version={self.SCHEMA_VERSION}")
            conn.execute(
                "INSERT OR IGNORE INTO metadata(key,value) VALUES(?,?)",
                ("coordination_policy_sha256_v1", self._policy_fingerprint),
            )
            policy_row = conn.execute(
                "SELECT value FROM metadata WHERE key=?",
                ("coordination_policy_sha256_v1",),
            ).fetchone()
            if policy_row is None or str(policy_row["value"]) != self._policy_fingerprint:
                raise ContractDriftError("coordination_policy_mismatch")
            confirmation_key = "write-confirmation-hmac-v1"
            conn.execute(
                "INSERT OR IGNORE INTO metadata(key,value) VALUES(?,?)",
                (
                    confirmation_key,
                    base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
                ),
            )
            confirmation_row = conn.execute(
                "SELECT value FROM metadata WHERE key=?",
                (confirmation_key,),
            ).fetchone()
            try:
                confirmation_secret = base64.urlsafe_b64decode(str(confirmation_row["value"]).encode())
            except Exception as exc:
                raise ContractDriftError("write_confirmation_secret_invalid") from exc
            if len(confirmation_secret) != 32:
                raise ContractDriftError("write_confirmation_secret_invalid")
            self._write_confirmation_secret = confirmation_secret
            prepared_rows = conn.execute(
                """
                SELECT write_id,scope,payload_hash,expires_at
                FROM writes WHERE state=?
                """,
                (WriteState.PREPARED.value,),
            ).fetchall()
            for prepared_row in prepared_rows:
                token = self._confirmation_token(
                    write_id=str(prepared_row["write_id"]),
                    scope=str(prepared_row["scope"]),
                    payload_hash=str(prepared_row["payload_hash"]),
                    expires_at=float(prepared_row["expires_at"]),
                )
                conn.execute(
                    "UPDATE writes SET confirmation_hash=? WHERE write_id=?",
                    (
                        hashlib.sha256(token.encode()).hexdigest(),
                        str(prepared_row["write_id"]),
                    ),
                )
        os.chmod(self._path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        info = self._path.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or (hasattr(os, "geteuid") and info.st_uid != os.geteuid())
        ):
            raise GatewayError(
                ErrorCode.VALIDATION,
                diagnostic="coordination_db_security_changed",
            )
        conn = sqlite3.connect(
            self._path,
            timeout=10,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA secure_delete=ON")
        return conn

    async def _async(self, fn: Callable[[], ResultT]) -> ResultT:
        return await asyncio.to_thread(fn)

    def _acquire_writer_lock(self, scope: str) -> int:
        digest = hashlib.sha256(scope.encode()).hexdigest()
        path = self._state_dir / f"writer-{digest}.lock"
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = -1
        try:
            fd = os.open(path, flags, 0o600)
            os.fchmod(fd, 0o600)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise GatewayError(
                    ErrorCode.VALIDATION,
                    diagnostic="writer_lock_not_regular",
                )
            if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
                raise GatewayError(
                    ErrorCode.VALIDATION,
                    diagnostic="writer_lock_wrong_owner",
                )
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise GatewayError(
                    ErrorCode.WRITE_IN_PROGRESS,
                    retryable=True,
                    safe_to_retry=True,
                    retry_after_seconds=1.0,
                    diagnostic="account_writer_process_lock_active",
                ) from exc
            return fd
        except Exception:
            if fd >= 0:
                os.close(fd)
            raise

    @staticmethod
    def _release_writer_lock(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    @asynccontextmanager
    async def writer_guard(self, scope: str) -> AsyncIterator[None]:
        """Hold a crash-safe per-account process lock around remote write work."""

        async with cancellation_safe_fd_guard(
            lambda: self._acquire_writer_lock(scope),
            self._release_writer_lock,
        ):
            yield

    def _transaction(self, fn: Callable[[sqlite3.Connection], ResultT]) -> ResultT:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn(conn)
            except BaseException:
                conn.rollback()
                raise
            conn.commit()
            return result

    async def get_or_create_secret(self, key: str, byte_length: int = 32) -> bytes:
        def operation(conn: sqlite3.Connection) -> bytes:
            row = conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
            if row is not None:
                return base64.urlsafe_b64decode(row["value"].encode())
            value = secrets.token_bytes(byte_length)
            encoded = base64.urlsafe_b64encode(value).decode()
            conn.execute("INSERT INTO metadata(key, value) VALUES (?, ?)", (key, encoded))
            return value

        return await self._async(lambda: self._transaction(operation))

    async def check_circuit(self, scope: str, route: str) -> None:
        now = time.time()

        def operation(conn: sqlite3.Connection) -> tuple[float, int] | None:
            row = conn.execute(
                """
                SELECT failures,open_until,updated_at,probe_until
                FROM circuits WHERE scope=? AND route=?
                """,
                (scope, route),
            ).fetchone()
            if row is None:
                return None
            open_until = float(row["open_until"])
            if not math.isfinite(open_until):
                open_until = now + 900.0
                conn.execute(
                    "UPDATE circuits SET open_until=?,updated_at=? WHERE scope=? AND route=?",
                    (open_until, now, scope, route),
                )
            failures = int(row["failures"])
            if open_until > now:
                return open_until, failures
            was_open = open_until > float(row["updated_at"]) + 1e-6
            if not was_open:
                return None
            probe_until = float(row["probe_until"])
            if probe_until > now:
                return probe_until, failures
            probe_window = max(self._config.timeout + 5.0, 10.0)
            conn.execute(
                """
                UPDATE circuits SET probe_owner=?,probe_until=?
                WHERE scope=? AND route=?
                """,
                (self._instance_id, now + probe_window, scope, route),
            )
            return None

        state = await self._async(lambda: self._transaction(operation))
        if state is not None:
            open_until, failures = state
            raise GatewayError(
                ErrorCode.CIRCUIT_OPEN,
                retryable=True,
                safe_to_retry=True,
                retry_after_seconds=min(900.0, max(0.0, open_until - now)),
                diagnostic=f"route={route};failures={failures}",
            )

    async def record_success(self, scope: str, route: str) -> None:
        def operation() -> None:
            with closing(self._connect()) as conn:
                conn.execute("DELETE FROM circuits WHERE scope=? AND route=?", (scope, route))

        await self._async(operation)

    async def record_failure(
        self,
        scope: str,
        route: str,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        now = time.time()

        def operation(conn: sqlite3.Connection) -> None:
            row = conn.execute(
                "SELECT failures FROM circuits WHERE scope=? AND route=?",
                (scope, route),
            ).fetchone()
            failures = (int(row["failures"]) if row is not None else 0) + 1
            open_for = 0.0
            if failures >= self._config.circuit_failure_threshold:
                exponent = min(
                    max(failures - self._config.circuit_failure_threshold, 0),
                    30,
                )
                open_for = min(
                    self._config.circuit_open_seconds * (2**exponent),
                    900.0,
                )
            if retry_after_seconds is not None and math.isfinite(retry_after_seconds):
                open_for = max(open_for, min(900.0, max(0.0, retry_after_seconds)))
            open_for = min(open_for, 900.0)
            conn.execute(
                """
                INSERT INTO circuits(scope, route, failures, open_until, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(scope, route) DO UPDATE SET
                    failures=excluded.failures,
                    open_until=excluded.open_until,
                    updated_at=excluded.updated_at,
                    probe_owner=NULL,
                    probe_until=0
                """,
                (scope, route, failures, now + open_for, now),
            )

        await self._async(lambda: self._transaction(operation))

    def _reserve_tokens(
        self,
        buckets: Sequence[tuple[str, float, int]],
        now: float,
    ) -> float:
        def operation(conn: sqlite3.Connection) -> float:
            states: list[tuple[str, float, float, float, int]] = []
            wait_for = 0.0
            for key, rate, capacity in buckets:
                row = conn.execute(
                    "SELECT tokens, last_refill FROM rate_buckets WHERE bucket_key=?",
                    (key,),
                ).fetchone()
                if row is None:
                    tokens = float(capacity)
                    last_refill = now
                else:
                    last_refill = min(float(row["last_refill"]), now)
                    tokens = min(
                        float(capacity),
                        float(row["tokens"]) + (now - last_refill) * rate,
                    )
                if tokens < 1.0:
                    wait_for = max(wait_for, (1.0 - tokens) / rate)
                states.append((key, tokens, now, rate, capacity))

            acquired = wait_for <= 0.0
            for key, tokens, timestamp, _rate, _capacity in states:
                updated_tokens = tokens - 1.0 if acquired else tokens
                conn.execute(
                    """
                    INSERT INTO rate_buckets(bucket_key, tokens, last_refill)
                    VALUES (?, ?, ?)
                    ON CONFLICT(bucket_key) DO UPDATE SET
                        tokens=excluded.tokens,
                        last_refill=excluded.last_refill
                    """,
                    (key, updated_tokens, timestamp),
                )
            return wait_for

        return self._transaction(operation)

    async def acquire(self, scope: str, route: str) -> None:
        deadline = time.monotonic() + self._config.rate_wait_timeout
        buckets = (
            (
                f"account:{scope}",
                self._config.rps_limit,
                self._config.burst_limit,
            ),
            (
                f"route:{scope}:{route}",
                self._config.route_rps_limit,
                self._config.route_burst_limit,
            ),
        )
        while True:
            wait_for = await self._async(lambda: self._reserve_tokens(buckets, time.time()))
            if wait_for <= 0:
                await self.check_circuit(scope, route)
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0 or wait_for > remaining:
                raise GatewayError(
                    ErrorCode.RATE_LIMIT,
                    retryable=True,
                    safe_to_retry=True,
                    retry_after_seconds=max(0.01, wait_for),
                    diagnostic=f"local_route={route}",
                )
            jitter = random.uniform(0.0, min(wait_for * 0.1, 0.05))
            await asyncio.sleep(min(wait_for + jitter, remaining))

    @staticmethod
    def canonical_payload(payload: dict[str, Any]) -> tuple[str, str]:
        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return payload_json, hashlib.sha256(payload_json.encode()).hexdigest()

    def _confirmation_token(
        self,
        *,
        write_id: str,
        scope: str,
        payload_hash: str,
        expires_at: float,
    ) -> str:
        message = "\x00".join(
            (
                "write-confirmation-v1",
                write_id,
                scope,
                payload_hash,
                f"{expires_at:.6f}",
            )
        ).encode()
        digest = hmac.new(
            self._write_confirmation_secret,
            message,
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

    @staticmethod
    def _row_to_write(row: sqlite3.Row) -> StoredWrite:
        return StoredWrite(
            write_id=str(row["write_id"]),
            scope=str(row["scope"]),
            idempotency_key=str(row["idempotency_key"]),
            action=WriteAction(str(row["action"])),
            payload_json=str(row["payload_json"]),
            payload_hash=str(row["payload_hash"]),
            state=WriteState(str(row["state"])),
            prepared_at=float(row["prepared_at"]),
            expires_at=float(row["expires_at"]),
            updated_at=float(row["updated_at"]),
            lease_owner=str(row["lease_owner"]) if row["lease_owner"] is not None else None,
            lease_expires=float(row["lease_expires"]) if row["lease_expires"] is not None else None,
            remote_started_at=(float(row["remote_started_at"]) if row["remote_started_at"] is not None else None),
            result_json=str(row["result_json"]) if row["result_json"] is not None else None,
            error_json=str(row["error_json"]) if row["error_json"] is not None else None,
        )

    async def prepare_write(
        self,
        *,
        scope: str,
        idempotency_key: str,
        action: WriteAction,
        payload: dict[str, Any],
    ) -> PreparedRecord:
        request_payload = payload.get("request")
        if not isinstance(request_payload, dict) or request_payload.get("action") != action.value:
            raise ContractDriftError("write_request_identity_invalid")
        payload_json, payload_hash = self.canonical_payload(payload)
        now = time.time()
        write_id = str(uuid.uuid4())
        expires_at = now + self._config.preparation_ttl_seconds
        confirmation_token = self._confirmation_token(
            write_id=write_id,
            scope=scope,
            payload_hash=payload_hash,
            expires_at=expires_at,
        )
        confirmation_hash = hashlib.sha256(confirmation_token.encode()).hexdigest()

        def operation(conn: sqlite3.Connection) -> PreparedRecord:
            retention_cutoff = now - self._config.state_retention_days * 86_400
            terminal_states = (
                WriteState.SUCCEEDED.value,
                WriteState.FAILED_KNOWN.value,
                WriteState.RECONCILED_SUCCEEDED.value,
                WriteState.RECONCILED_ABSENT.value,
                WriteState.EXPIRED.value,
            )
            placeholders = ",".join("?" for _ in terminal_states)
            old_ids = [
                str(item["write_id"])
                for item in conn.execute(
                    f"""
                    SELECT write_id FROM writes
                    WHERE updated_at < ? AND state IN ({placeholders})
                    """,
                    (retention_cutoff, *terminal_states),
                ).fetchall()
            ]
            if old_ids:
                old_placeholders = ",".join("?" for _ in old_ids)
                conn.execute(
                    f"DELETE FROM write_events WHERE write_id IN ({old_placeholders})",
                    old_ids,
                )
                conn.execute(
                    f"DELETE FROM writes WHERE write_id IN ({old_placeholders})",
                    old_ids,
                )
            row = conn.execute(
                "SELECT * FROM writes WHERE scope=? AND idempotency_key=?",
                (scope, idempotency_key),
            ).fetchone()
            if row is not None:
                existing = self._row_to_write(row)
                try:
                    existing_payload = json.loads(existing.payload_json)
                except json.JSONDecodeError as exc:
                    raise ContractDriftError("stored_write_payload_invalid") from exc
                existing_request = existing_payload.get("request") if isinstance(existing_payload, dict) else None
                same_request = isinstance(existing_request, dict) and self.canonical_payload(
                    existing_request
                ) == self.canonical_payload(request_payload)
                if existing.action is not action or not same_request:
                    raise GatewayError(
                        ErrorCode.IDEMPOTENCY_CONFLICT,
                        diagnostic="idempotency_payload_mismatch",
                    )
                if existing.state is WriteState.PREPARED and existing.expires_at <= now:
                    conn.execute(
                        "UPDATE writes SET state=?, updated_at=? WHERE write_id=?",
                        (WriteState.EXPIRED.value, now, existing.write_id),
                    )
                    conn.execute(
                        "INSERT INTO write_events(write_id,state,occurred_at,note) VALUES(?,?,?,?)",
                        (existing.write_id, WriteState.EXPIRED.value, now, "ttl"),
                    )
                    expired = conn.execute(
                        "SELECT * FROM writes WHERE write_id=?",
                        (existing.write_id,),
                    ).fetchone()
                    return PreparedRecord(self._row_to_write(expired), None)
                if existing.state is WriteState.PREPARED:
                    replay_token = self._confirmation_token(
                        write_id=existing.write_id,
                        scope=existing.scope,
                        payload_hash=existing.payload_hash,
                        expires_at=existing.expires_at,
                    )
                    return PreparedRecord(existing, replay_token)
                return PreparedRecord(existing, None)

            conn.execute(
                """
                INSERT INTO writes(
                    write_id,scope,idempotency_key,action,payload_json,payload_hash,
                    confirmation_hash,state,prepared_at,expires_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    write_id,
                    scope,
                    idempotency_key,
                    action.value,
                    payload_json,
                    payload_hash,
                    confirmation_hash,
                    WriteState.PREPARED.value,
                    now,
                    expires_at,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO write_events(write_id,state,occurred_at,note) VALUES(?,?,?,?)",
                (write_id, WriteState.PREPARED.value, now, "prepared"),
            )
            row = conn.execute("SELECT * FROM writes WHERE write_id=?", (write_id,)).fetchone()
            return PreparedRecord(self._row_to_write(row), confirmation_token)

        return await self._async(lambda: self._transaction(operation))

    async def get_write(self, write_id: str, *, scope: str) -> StoredWrite | None:
        now = time.time()

        def operation(conn: sqlite3.Connection) -> StoredWrite | None:
            row = conn.execute(
                "SELECT * FROM writes WHERE write_id=? AND scope=?",
                (write_id, scope),
            ).fetchone()
            if row is None:
                return None
            record = self._row_to_write(row)
            if record.state is WriteState.PREPARED and record.expires_at <= now:
                conn.execute(
                    "UPDATE writes SET state=?,updated_at=? WHERE write_id=? AND state=?",
                    (
                        WriteState.EXPIRED.value,
                        now,
                        write_id,
                        WriteState.PREPARED.value,
                    ),
                )
                conn.execute(
                    "INSERT INTO write_events(write_id,state,occurred_at,note) VALUES(?,?,?,?)",
                    (write_id, WriteState.EXPIRED.value, now, "ttl"),
                )
                row = conn.execute(
                    "SELECT * FROM writes WHERE write_id=?",
                    (write_id,),
                ).fetchone()
            return self._row_to_write(row) if row is not None else None

        return await self._async(lambda: self._transaction(operation))

    async def get_write_by_idempotency(
        self,
        *,
        scope: str,
        idempotency_key: str,
    ) -> StoredWrite | None:
        def operation() -> StoredWrite | None:
            with closing(self._connect()) as conn:
                row = conn.execute(
                    "SELECT * FROM writes WHERE scope=? AND idempotency_key=?",
                    (scope, idempotency_key),
                ).fetchone()
            return self._row_to_write(row) if row is not None else None

        return await self._async(operation)

    def _recover_stale_record(
        self,
        conn: sqlite3.Connection,
        record: StoredWrite,
        *,
        now: float,
        note: str,
    ) -> StoredWrite:
        if record.remote_started_at is None:
            target_state = WriteState.PREPARED if record.expires_at > now else WriteState.EXPIRED
            error_json = None
            event_note = f"{note}_before_remote"
        else:
            target_state = WriteState.SUBMISSION_UNKNOWN
            error_json = json.dumps(
                {
                    "code": ErrorCode.AMBIGUOUS_WRITE.value,
                    "message": "writer lease expired after remote boundary",
                },
                sort_keys=True,
            )
            event_note = f"{note}_after_remote"
        conn.execute(
            """
            UPDATE writes SET state=?,updated_at=?,lease_owner=NULL,
                lease_expires=NULL,error_json=?
            WHERE write_id=? AND scope=? AND state=?
            """,
            (
                target_state.value,
                now,
                error_json,
                record.write_id,
                record.scope,
                WriteState.COMMITTING.value,
            ),
        )
        conn.execute(
            """
            INSERT INTO write_events(write_id,state,occurred_at,note)
            VALUES(?,?,?,?)
            """,
            (record.write_id, target_state.value, now, event_note),
        )
        row = conn.execute(
            "SELECT * FROM writes WHERE write_id=?",
            (record.write_id,),
        ).fetchone()
        return self._row_to_write(row)

    async def recover_stale_write(
        self,
        *,
        write_id: str,
        scope: str,
    ) -> StoredWrite | None:
        """Recover an expired lease according to its durable remote marker.

        The caller must hold :meth:`writer_guard` for ``scope``. That process lock
        distinguishes a crashed writer from a live request that merely exceeded
        its advisory database lease.
        """

        now = time.time()

        def operation(conn: sqlite3.Connection) -> StoredWrite | None:
            row = conn.execute(
                "SELECT * FROM writes WHERE write_id=? AND scope=?",
                (write_id, scope),
            ).fetchone()
            if row is None:
                return None
            record = self._row_to_write(row)
            if (
                record.state is WriteState.COMMITTING
                and record.lease_expires is not None
                and record.lease_expires <= now
            ):
                return self._recover_stale_record(
                    conn,
                    record,
                    now=now,
                    note="stale_writer_recovered",
                )
            return self._row_to_write(row)

        return await self._async(lambda: self._transaction(operation))

    async def claim_write(
        self,
        *,
        write_id: str,
        scope: str,
        payload_hash: str,
        confirmation_token: str,
        owner: str,
    ) -> StoredWrite:
        now = time.time()
        confirmation_hash = hashlib.sha256(confirmation_token.encode()).hexdigest()

        def operation(
            conn: sqlite3.Connection,
        ) -> tuple[StoredWrite, GatewayError | None]:
            row = conn.execute(
                "SELECT * FROM writes WHERE write_id=? AND scope=?",
                (write_id, scope),
            ).fetchone()
            if row is None:
                raise GatewayError(ErrorCode.NOT_FOUND, diagnostic="write_id_not_found")
            record = self._row_to_write(row)
            if not hmac.compare_digest(record.payload_hash, payload_hash) or not hmac.compare_digest(
                str(row["confirmation_hash"]),
                confirmation_hash,
            ):
                raise GatewayError(
                    ErrorCode.INVALID_CONFIRMATION,
                    diagnostic="write_confirmation_mismatch",
                )
            if (
                record.state is WriteState.COMMITTING
                and record.lease_expires is not None
                and record.lease_expires <= now
            ):
                record = self._recover_stale_record(
                    conn,
                    record,
                    now=now,
                    note="stale_writer",
                )
            if record.state is WriteState.PREPARED:
                if record.expires_at <= now:
                    conn.execute(
                        "UPDATE writes SET state=?,updated_at=? WHERE write_id=?",
                        (WriteState.EXPIRED.value, now, write_id),
                    )
                    conn.execute(
                        "INSERT INTO write_events(write_id,state,occurred_at,note) VALUES(?,?,?,?)",
                        (write_id, WriteState.EXPIRED.value, now, "ttl"),
                    )
                    expired = conn.execute(
                        "SELECT * FROM writes WHERE write_id=?",
                        (write_id,),
                    ).fetchone()
                    return self._row_to_write(expired), None
                unknown_row = conn.execute(
                    """
                    SELECT write_id FROM writes
                    WHERE scope=? AND state=? AND write_id<>?
                    LIMIT 1
                    """,
                    (
                        record.scope,
                        WriteState.SUBMISSION_UNKNOWN.value,
                        record.write_id,
                    ),
                ).fetchone()
                if unknown_row is not None:
                    return record, GatewayError(
                        ErrorCode.AMBIGUOUS_WRITE,
                        retryable=False,
                        safe_to_retry=False,
                        reconciliation_required=True,
                        diagnostic="unresolved_account_write_barrier",
                    )
                active_row = conn.execute(
                    """
                    SELECT * FROM writes
                    WHERE scope=? AND state=? AND write_id<>?
                    LIMIT 1
                    """,
                    (
                        record.scope,
                        WriteState.COMMITTING.value,
                        record.write_id,
                    ),
                ).fetchone()
                if active_row is not None:
                    active = self._row_to_write(active_row)
                    if active.lease_expires is None or active.lease_expires > now:
                        raise GatewayError(
                            ErrorCode.WRITE_IN_PROGRESS,
                            retryable=True,
                            safe_to_retry=True,
                            retry_after_seconds=max(
                                0.0,
                                (active.lease_expires or now) - now,
                            ),
                            diagnostic="account_writer_lease_active",
                        )
                    recovered_active = self._recover_stale_record(
                        conn,
                        active,
                        now=now,
                        note="stale_writer",
                    )
                    if recovered_active.state is WriteState.SUBMISSION_UNKNOWN:
                        return record, GatewayError(
                            ErrorCode.AMBIGUOUS_WRITE,
                            retryable=False,
                            safe_to_retry=False,
                            reconciliation_required=True,
                            diagnostic="stale_writer_became_unknown",
                        )
                lease_expires = now + self._config.write_lease_seconds
                conn.execute(
                    """
                    UPDATE writes SET state=?,updated_at=?,lease_owner=?,
                        lease_expires=?,remote_started_at=NULL
                    WHERE write_id=? AND state=?
                    """,
                    (
                        WriteState.COMMITTING.value,
                        now,
                        owner,
                        lease_expires,
                        write_id,
                        WriteState.PREPARED.value,
                    ),
                )
                conn.execute(
                    "INSERT INTO write_events(write_id,state,occurred_at,note) VALUES(?,?,?,?)",
                    (write_id, WriteState.COMMITTING.value, now, "claimed"),
                )
                claimed = conn.execute(
                    "SELECT * FROM writes WHERE write_id=?",
                    (write_id,),
                ).fetchone()
                return self._row_to_write(claimed), None
            if record.state is WriteState.COMMITTING:
                raise GatewayError(
                    ErrorCode.WRITE_IN_PROGRESS,
                    retryable=True,
                    safe_to_retry=True,
                    retry_after_seconds=max(0.0, (record.lease_expires or now) - now),
                    diagnostic="active_writer_lease",
                )
            return record, None

        claimed, barrier = await self._async(lambda: self._transaction(operation))
        if barrier is not None:
            raise barrier
        return claimed

    async def mark_write_remote_started(
        self,
        *,
        write_id: str,
        scope: str,
        owner: str,
    ) -> StoredWrite:
        """Durably cross the no-retry boundary immediately before upstream I/O."""

        now = time.time()

        def operation(conn: sqlite3.Connection) -> StoredWrite:
            row = conn.execute(
                "SELECT * FROM writes WHERE write_id=? AND scope=?",
                (write_id, scope),
            ).fetchone()
            if row is None:
                raise GatewayError(
                    ErrorCode.NOT_FOUND,
                    diagnostic="write_id_not_found",
                )
            record = self._row_to_write(row)
            if record.state is not WriteState.COMMITTING or record.lease_owner != owner:
                raise AmbiguousWriteError("writer_lost_lease_before_remote")
            if record.remote_started_at is not None:
                return record
            conn.execute(
                """
                UPDATE writes SET remote_started_at=?,updated_at=?
                WHERE write_id=? AND scope=? AND state=? AND lease_owner=?
                """,
                (
                    now,
                    now,
                    write_id,
                    scope,
                    WriteState.COMMITTING.value,
                    owner,
                ),
            )
            conn.execute(
                """
                INSERT INTO write_events(write_id,state,occurred_at,note)
                VALUES(?,?,?,?)
                """,
                (
                    write_id,
                    WriteState.COMMITTING.value,
                    now,
                    "remote_boundary_started",
                ),
            )
            updated = conn.execute(
                "SELECT * FROM writes WHERE write_id=?",
                (write_id,),
            ).fetchone()
            return self._row_to_write(updated)

        return await self._async(lambda: self._transaction(operation))

    async def finish_write(
        self,
        *,
        write_id: str,
        scope: str,
        owner: str,
        state: WriteState,
        result: dict[str, Any] | None = None,
        error: ErrorInfo | None = None,
        note: str,
    ) -> StoredWrite:
        allowed = {
            WriteState.SUCCEEDED,
            WriteState.FAILED_KNOWN,
            WriteState.SUBMISSION_UNKNOWN,
        }
        if state not in allowed:
            raise ValueError(f"invalid finish state: {state}")
        now = time.time()
        result_json = (
            json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if result is not None
            else None
        )
        error_json = error.model_dump_json(exclude_none=True, by_alias=True) if error is not None else None

        def operation(conn: sqlite3.Connection) -> StoredWrite:
            row = conn.execute(
                "SELECT * FROM writes WHERE write_id=? AND scope=?",
                (write_id, scope),
            ).fetchone()
            if row is None:
                raise GatewayError(ErrorCode.NOT_FOUND, diagnostic="write_id_not_found")
            record = self._row_to_write(row)
            if record.state is not WriteState.COMMITTING or record.lease_owner != owner:
                raise AmbiguousWriteError("writer_lost_lease")
            conn.execute(
                """
                UPDATE writes SET state=?,updated_at=?,lease_owner=NULL,lease_expires=NULL,
                    result_json=?,error_json=?
                WHERE write_id=?
                """,
                (state.value, now, result_json, error_json, write_id),
            )
            conn.execute(
                "INSERT INTO write_events(write_id,state,occurred_at,note) VALUES(?,?,?,?)",
                (write_id, state.value, now, note),
            )
            updated = conn.execute("SELECT * FROM writes WHERE write_id=?", (write_id,)).fetchone()
            return self._row_to_write(updated)

        return await self._async(lambda: self._transaction(operation))

    async def release_write_claim(
        self,
        *,
        write_id: str,
        scope: str,
        owner: str,
        note: str,
    ) -> StoredWrite:
        """Return a claim to ``prepared`` when no remote write was attempted."""

        now = time.time()

        def operation(conn: sqlite3.Connection) -> StoredWrite:
            row = conn.execute(
                "SELECT * FROM writes WHERE write_id=? AND scope=?",
                (write_id, scope),
            ).fetchone()
            if row is None:
                raise GatewayError(ErrorCode.NOT_FOUND, diagnostic="write_id_not_found")
            record = self._row_to_write(row)
            if record.state is not WriteState.COMMITTING or record.lease_owner != owner:
                raise AmbiguousWriteError("writer_lost_lease_before_release")
            if record.remote_started_at is not None:
                raise AmbiguousWriteError("remote_boundary_already_started")
            state = WriteState.PREPARED if record.expires_at > now else WriteState.EXPIRED
            conn.execute(
                """
                UPDATE writes SET state=?,updated_at=?,lease_owner=NULL,lease_expires=NULL
                WHERE write_id=?
                """,
                (state.value, now, write_id),
            )
            conn.execute(
                "INSERT INTO write_events(write_id,state,occurred_at,note) VALUES(?,?,?,?)",
                (write_id, state.value, now, note),
            )
            updated = conn.execute("SELECT * FROM writes WHERE write_id=?", (write_id,)).fetchone()
            return self._row_to_write(updated)

        return await self._async(lambda: self._transaction(operation))

    async def reconcile_write(
        self,
        *,
        write_id: str,
        scope: str,
        state: WriteState,
        result: dict[str, Any] | None,
        note: str,
    ) -> StoredWrite:
        if state not in {
            WriteState.RECONCILED_SUCCEEDED,
            WriteState.RECONCILED_ABSENT,
        }:
            raise ValueError(f"invalid reconciliation state: {state}")
        now = time.time()
        result_json = (
            json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if result is not None
            else None
        )

        def operation(conn: sqlite3.Connection) -> StoredWrite:
            row = conn.execute(
                "SELECT * FROM writes WHERE write_id=? AND scope=?",
                (write_id, scope),
            ).fetchone()
            if row is None:
                raise GatewayError(ErrorCode.NOT_FOUND, diagnostic="write_id_not_found")
            record = self._row_to_write(row)
            if record.state is not WriteState.SUBMISSION_UNKNOWN:
                return record
            min_age = self._config.reconciliation_min_age_seconds
            if now - record.updated_at < min_age:
                raise GatewayError(
                    ErrorCode.WRITE_IN_PROGRESS,
                    retryable=True,
                    safe_to_retry=True,
                    retry_after_seconds=min_age - (now - record.updated_at),
                    diagnostic="reconciliation_visibility_window",
                )
            if state is WriteState.RECONCILED_ABSENT:
                observation = conn.execute(
                    """
                    SELECT COUNT(*) AS count, MAX(occurred_at) AS last_observed
                    FROM write_events
                    WHERE write_id=? AND note='read_back_absent_observation'
                    """,
                    (write_id,),
                ).fetchone()
                count = int(observation["count"])
                last_observed_raw = observation["last_observed"]
                last_observed = float(last_observed_raw) if last_observed_raw is not None else None
                interval = self._config.reconciliation_absence_interval_seconds
                if last_observed is not None and now - last_observed < interval:
                    raise GatewayError(
                        ErrorCode.WRITE_IN_PROGRESS,
                        retryable=True,
                        safe_to_retry=True,
                        retry_after_seconds=interval - (now - last_observed),
                        diagnostic="reconciliation_absence_interval",
                    )
                conn.execute(
                    """
                    INSERT INTO write_events(write_id,state,occurred_at,note)
                    VALUES(?,?,?,?)
                    """,
                    (
                        write_id,
                        WriteState.SUBMISSION_UNKNOWN.value,
                        now,
                        "read_back_absent_observation",
                    ),
                )
                if count + 1 < self._config.reconciliation_absence_confirmations:
                    return record
            conn.execute(
                """
                UPDATE writes SET state=?,updated_at=?,result_json=?,error_json=NULL
                WHERE write_id=?
                """,
                (state.value, now, result_json, write_id),
            )
            conn.execute(
                "INSERT INTO write_events(write_id,state,occurred_at,note) VALUES(?,?,?,?)",
                (write_id, state.value, now, note),
            )
            updated = conn.execute("SELECT * FROM writes WHERE write_id=?", (write_id,)).fetchone()
            return self._row_to_write(updated)

        return await self._async(lambda: self._transaction(operation))

    async def count_events(self, write_id: str) -> int:
        def operation() -> int:
            with closing(self._connect()) as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS count FROM write_events WHERE write_id=?",
                    (write_id,),
                ).fetchone()
            return int(row["count"])

        return await self._async(operation)


class CursorCodec:
    """Opaque HMAC-bound cursors tied to an exact filter fingerprint."""

    def __init__(self, store: CoordinationStore) -> None:
        self._store = store

    async def encode(self, payload: dict[str, Any]) -> str:
        secret = await self._store.get_or_create_secret("cursor-hmac-v1")
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        signature = hmac.new(secret, body, hashlib.sha256).digest()
        return (
            base64.urlsafe_b64encode(body).rstrip(b"=").decode()
            + "."
            + base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
        )

    async def decode(self, cursor: str) -> dict[str, Any]:
        try:
            encoded_body, encoded_signature = cursor.split(".", 1)
            body = base64.urlsafe_b64decode(encoded_body + "=" * (-len(encoded_body) % 4))
            signature = base64.urlsafe_b64decode(encoded_signature + "=" * (-len(encoded_signature) % 4))
            secret = await self._store.get_or_create_secret("cursor-hmac-v1")
            expected = hmac.new(secret, body, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError("signature")
            payload = json.loads(body)
            if not isinstance(payload, dict) or payload.get("v") != 1:
                raise ValueError("version")
            return payload
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise GatewayError(ErrorCode.VALIDATION, diagnostic="invalid_cursor") from exc


def pages_from_paging(paging: dict[str, Any], item_count: int) -> tuple[int, int | None, int | None, int | None]:
    page_raw = paging.get("page")
    page = (
        int(page_raw) if isinstance(page_raw, int | float) and not isinstance(page_raw, bool) and page_raw >= 1 else 1
    )
    limit_raw = paging.get("limit")
    total_raw = paging.get("total")
    limit = (
        int(limit_raw)
        if isinstance(limit_raw, int | float) and not isinstance(limit_raw, bool) and limit_raw > 0
        else None
    )
    total = (
        int(total_raw)
        if isinstance(total_raw, int | float) and not isinstance(total_raw, bool) and total_raw >= 0
        else None
    )
    pages_raw = paging.get("pages")
    if isinstance(pages_raw, int | float) and not isinstance(pages_raw, bool) and pages_raw >= 0:
        pages = int(pages_raw)
    elif limit and total is not None:
        pages = math.ceil(total / limit)
    else:
        pages = None
    if limit is None and item_count:
        limit = item_count
    return page, limit, total, pages
