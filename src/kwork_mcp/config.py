from __future__ import annotations

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class KworkConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KWORK_",
        env_file=".env",
        extra="ignore",
    )

    login: str = ""
    password: SecretStr = SecretStr("")
    phone_last: str | None = None
    token: SecretStr | None = None

    proxy_url: str | None = None
    timeout: int = 30

    rps_limit: int = 2
    burst_limit: int = 5

    token_file: Path = Path.home() / ".kwork_token"
