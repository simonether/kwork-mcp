from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from kwork.schema.actor import Actor

from kwork_mcp.config import KworkConfig
from kwork_mcp.coordination import CoordinationStore


@pytest.fixture
def config_factory(tmp_path: Path) -> Callable[..., KworkConfig]:
    counter = 0

    def factory(**overrides: Any) -> KworkConfig:
        nonlocal counter
        counter += 1
        state_dir = tmp_path / f"state-{counter}"
        os.mkdir(state_dir, mode=0o700)
        values: dict[str, Any] = {
            "token": "fixture-token",
            "state_dir": state_dir,
            "persist_token": False,
            "rps_limit": 100.0,
            "burst_limit": 100,
            "route_rps_limit": 100.0,
            "route_burst_limit": 100,
            "rate_wait_timeout": 2.0,
            "retry_backoff_base": 0.0,
            "retry_backoff_max": 0.0,
            "reconciliation_min_age_seconds": 1.0,
        }
        values.update(overrides)
        return KworkConfig(**values)

    return factory


@pytest.fixture
def coordinator(config_factory: Callable[..., KworkConfig]) -> CoordinationStore:
    return CoordinationStore(config_factory())


def actor(user_id: int = 42, username: str = "fixture-user") -> Actor:
    return Actor(id=user_id, username=username)
