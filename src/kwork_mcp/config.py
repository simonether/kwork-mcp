"""Explicit environment-only configuration.

No ``.env`` file is loaded implicitly.  This matters in Codex because the
working directory is untrusted application input, not a configuration root.
"""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from collections.abc import Mapping
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from yarl import URL

_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")


def _default_state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    if base:
        return Path(base).expanduser() / "kwork-mcp"
    return Path.home() / ".local" / "state" / "kwork-mcp"


def contains_unsafe_text_codepoint(value: str) -> bool:
    """Reject terminal controls, bidi/format controls, and invalid surrogates."""

    return any(unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for char in value)


SERVER_SECRET_ENV_NAMES = frozenset(
    {
        "KWORK_LOGIN",
        "KWORK_PASSWORD",
        "KWORK_PHONE_LAST",
        "KWORK_PROXY_URL",
        "KWORK_TOKEN",
    }
)


def canonicalize_percent_escape_case(value: str) -> str:
    """Normalize only hexadecimal digits in percent escapes without resizing."""

    return _PERCENT_ESCAPE.sub(
        lambda match: match.group(0).upper(),
        value,
    )


def normalize_proxy_url(value: str) -> str:
    """Validate one proxy URL without exposing it in an exception."""

    normalized = value.strip()
    if not normalized:
        return ""
    if contains_unsafe_text_codepoint(normalized):
        raise ValueError("proxy URL contains control characters")
    if len(normalized.encode("utf-8")) > 8192:
        raise ValueError("proxy URL is too large")
    parsed = urlsplit(normalized)
    if parsed.scheme.lower() not in {"http", "https", "socks4", "socks5"}:
        raise ValueError("proxy URL scheme must be http, https, socks4, or socks5")
    if not parsed.hostname:
        raise ValueError("proxy URL must contain a hostname")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("proxy URL contains an invalid port") from exc
    if port is not None and not 1 <= port <= 65_535:
        raise ValueError("proxy URL port must be between 1 and 65535")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("proxy URL cannot contain a path, query, or fragment")
    return normalized


def proxy_redaction_secrets(value: str) -> tuple[str, ...]:
    """Return raw and decoded proxy credential forms without logging them."""

    parsed = urlsplit(value)
    raw_userinfo = parsed.netloc.rsplit("@", 1)[0] if "@" in parsed.netloc else ""
    base_candidates = [
        value,
        raw_userinfo,
        parsed.username or "",
        parsed.password or "",
    ]
    canonical = URL(value)
    base_candidates.extend(
        [
            str(canonical),
            canonical.raw_authority,
            canonical.raw_host or "",
            canonical.host or "",
            canonical.raw_user or "",
            canonical.user or "",
            canonical.raw_password or "",
            canonical.password or "",
        ]
    )
    candidates: list[str] = []
    for candidate in base_candidates:
        for form in (candidate, unquote(candidate)):
            candidates.extend(
                (
                    form,
                    canonicalize_percent_escape_case(form),
                )
            )
    return tuple(dict.fromkeys(candidate for candidate in candidates if candidate))


def secret_server_environment_present(
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Whether a normal MCP launch inherited any secret-bearing Kwork setting."""

    source = os.environ if environ is None else environ
    return any(key.upper() in SERVER_SECRET_ENV_NAMES and bool(value.strip()) for key, value in source.items())


def validate_steady_state_server_config(config: KworkConfig) -> KworkConfig:
    """Reject every credential source that belongs exclusively to bootstrap."""

    if config.token_value or config.login or config.password_value or config.phone_last_value or config.proxy_value:
        raise ValueError("secret-bearing configuration is forbidden for the normal MCP server; use kwork-mcp-bootstrap")
    if config.expected_user_id is None or not config.persist_token:
        raise ValueError("normal MCP server requires KWORK_EXPECTED_USER_ID and KWORK_PERSIST_TOKEN=true")
    return config


class KworkConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KWORK_",
        env_file=None,
        env_ignore_empty=True,
        extra="ignore",
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    login: str = ""
    password: SecretStr = SecretStr("")
    phone_last: SecretStr | None = None
    token: SecretStr | None = None

    expected_user_id: int | None = Field(default=None, gt=0)
    expected_username: str | None = None
    enable_writes: bool = False

    proxy_url: SecretStr | None = None
    timeout: float = Field(default=30.0, ge=1.0, le=120.0)

    state_dir: Path = Field(default_factory=_default_state_dir)
    persist_token: bool = True
    token_file: str | None = None

    rps_limit: float = Field(default=2.0, gt=0.0, le=100.0)
    burst_limit: int = Field(default=4, ge=1, le=100)
    route_rps_limit: float = Field(default=1.0, gt=0.0, le=100.0)
    route_burst_limit: int = Field(default=2, ge=1, le=100)
    rate_wait_timeout: float = Field(default=20.0, ge=0.1, le=120.0)
    auth_lock_timeout: float = Field(default=90.0, ge=5.0, le=300.0)

    read_attempts: int = Field(default=3, ge=1, le=5)
    retry_backoff_base: float = Field(default=0.5, ge=0.0, le=30.0)
    retry_backoff_max: float = Field(default=8.0, ge=0.0, le=120.0)
    circuit_failure_threshold: int = Field(default=3, ge=1, le=20)
    circuit_open_seconds: float = Field(default=30.0, ge=1.0, le=900.0)

    preparation_ttl_seconds: int = Field(default=600, ge=30, le=3600)
    write_lease_seconds: int = Field(default=120, ge=10, le=900)
    reconciliation_min_age_seconds: float = Field(default=15.0, ge=1.0, le=300.0)
    reconciliation_absence_confirmations: int = Field(default=2, ge=2, le=5)
    reconciliation_absence_interval_seconds: float = Field(
        default=15.0,
        ge=1.0,
        le=300.0,
    )
    state_retention_days: int = Field(default=30, ge=1, le=3650)

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("login")
    @classmethod
    def validate_login(cls, value: str) -> str:
        normalized = value.strip()
        if contains_unsafe_text_codepoint(normalized):
            raise ValueError("control characters are not allowed")
        if len(normalized.encode("utf-8")) > 1024:
            raise ValueError("login is too large")
        return normalized

    @field_validator("expected_username")
    @classmethod
    def validate_expected_username(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lstrip("@").strip()
        if not normalized:
            return None
        if contains_unsafe_text_codepoint(normalized):
            raise ValueError("control characters are not allowed")
        if len(normalized.encode("utf-8")) > 1024:
            raise ValueError("expected_username is too large")
        return normalized

    @field_validator("token")
    @classmethod
    def validate_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        raw = value.get_secret_value()
        if not raw:
            return None
        if contains_unsafe_text_codepoint(raw) or len(raw.encode("utf-8")) > 16_384:
            raise ValueError("token is invalid")
        return SecretStr(raw)

    @field_validator("phone_last")
    @classmethod
    def validate_phone_last(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        raw = value.get_secret_value().strip()
        if raw and (not raw.isdigit() or len(raw) != 4):
            raise ValueError("phone_last must contain exactly four digits")
        return SecretStr(raw) if raw else None

    @field_validator("proxy_url")
    @classmethod
    def validate_proxy(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        raw = normalize_proxy_url(value.get_secret_value())
        if not raw:
            return None
        return SecretStr(raw)

    @field_validator("state_dir")
    @classmethod
    def validate_state_dir(cls, value: Path) -> Path:
        expanded = value.expanduser()
        if not expanded.is_absolute():
            raise ValueError("state_dir must be an absolute path")
        return expanded

    @model_validator(mode="after")
    def validate_auth_and_legacy_options(self) -> KworkConfig:
        password_set = bool(self.password.get_secret_value())
        if bool(self.login) != password_set:
            raise ValueError("KWORK_LOGIN and KWORK_PASSWORD must be provided together")
        if (
            not self.token_value
            and not (self.login and password_set)
            and (self.expected_user_id is None or not self.persist_token)
        ):
            raise ValueError("credentialless startup requires KWORK_EXPECTED_USER_ID and KWORK_PERSIST_TOKEN=true")
        if self.token_file is not None:
            raise ValueError(
                "KWORK_TOKEN_FILE was removed in 1.0; use KWORK_STATE_DIR for the secured account-scoped store"
            )
        if self.enable_writes and self.expected_user_id is None:
            raise ValueError("KWORK_ENABLE_WRITES requires KWORK_EXPECTED_USER_ID")
        if self.retry_backoff_max < self.retry_backoff_base:
            raise ValueError("retry_backoff_max must be >= retry_backoff_base")
        return self

    @property
    def token_value(self) -> str:
        return self.token.get_secret_value() if self.token is not None else ""

    @property
    def password_value(self) -> str:
        return self.password.get_secret_value()

    @property
    def phone_last_value(self) -> str | None:
        return self.phone_last.get_secret_value() if self.phone_last is not None else None

    @property
    def proxy_value(self) -> str | None:
        return self.proxy_url.get_secret_value() if self.proxy_url is not None else None

    @property
    def redaction_secrets(self) -> tuple[str, ...]:
        values = [
            self.token_value,
            self.password_value,
            self.login,
            self.phone_last_value or "",
        ]
        if self.proxy_value:
            values.extend(proxy_redaction_secrets(self.proxy_value))
        return tuple(dict.fromkeys(value for value in values if value))

    @property
    def bootstrap_scope(self) -> str:
        if self.expected_user_id is not None:
            return f"account-{self.expected_user_id}"
        if self.expected_username:
            seed = f"username:{self.expected_username.casefold()}"
        elif self.login:
            seed = f"login:{self.login.casefold()}"
        else:
            seed = f"token:{self.token_value}"
        return "unbound-" + hashlib.sha256(seed.encode()).hexdigest()[:20]

    @property
    def token_cache_is_bound(self) -> bool:
        """Whether a persisted token has an independent identity namespace."""

        return bool(self.expected_user_id or self.expected_username or self.login)

    @property
    def fresh_credentials_available(self) -> bool:
        return bool(self.login and self.password_value)
