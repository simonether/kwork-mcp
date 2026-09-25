"""Shared state for the gateway layers."""

from __future__ import annotations

import uuid

from kwork_mcp.config import KworkConfig
from kwork_mcp.coordination import CoordinationStore, CursorCodec
from kwork_mcp.session import KworkSessionManager


class GatewayBase:
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
