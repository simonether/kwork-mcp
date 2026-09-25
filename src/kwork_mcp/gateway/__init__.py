"""Typed Kwork API gateway and durable prepare/commit/reconcile orchestration.

The gateway is layered; each module builds on the one before it:

``parsing``     pure envelope, paging and scalar helpers
``base``        shared state (config, coordinator, session)
``reads``       typed read operations
``lookups``     exhaustive scans used by preflight and reconciliation
``offer_flow``  multi-step web flow that submits an exchange offer
``actions``     per-action preflight / execute / read-back rules
``writes``      durable prepare → commit → reconcile protocol
"""

from __future__ import annotations

from kwork_mcp.gateway.writes import WriteProtocol

__all__ = ["KworkGateway"]


class KworkGateway(WriteProtocol):
    """Complete gateway: typed reads plus the durable write protocol."""
