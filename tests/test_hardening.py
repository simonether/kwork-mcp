"""Startup, proxy and redaction hardening."""

from __future__ import annotations

import io
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import kwork_mcp
from kwork_mcp.bootstrap import run_bootstrap_cli
from kwork_mcp.config import normalize_proxy_url, proxy_redaction_secrets
from kwork_mcp.security import sanitize_external

# --- redaction never corrupts structure or ordinary text


def test_short_proxy_fragments_do_not_corrupt_keys_or_text() -> None:
    secrets = proxy_redaction_secrets("http://user:pw@10.0.0.5:3128")

    clean = sanitize_external(
        {"user_id": 7, "username": "client", "message": "see the user guide"},
        secrets=secrets,
    )

    assert clean == {"user_id": 7, "username": "client", "message": "see the user guide"}


def test_proxy_url_and_long_credentials_are_still_redacted() -> None:
    url = "http://proxyadmin:S3cretPassw0rd@proxy.example.net:3128"
    secrets = proxy_redaction_secrets(url)

    clean = sanitize_external(
        {"note": f"via {url}", "echo": "S3cretPassw0rd", "token": "t"},
        secrets=secrets,
    )

    rendered = repr(clean)
    assert "S3cretPassw0rd" not in rendered
    assert "proxyadmin" not in rendered
    assert clean["token"] == "<redacted>"


def test_registered_secrets_including_short_passwords_are_removed_from_keys() -> None:
    secrets = proxy_redaction_secrets("http://user:pw@10.0.0.5:3128")

    clean = sanitize_external({"pw": "value", "user_id": 1}, secrets=secrets)

    assert clean == {"<redacted>": "value", "user_id": 1}


# --- proxy URLs are validated exactly as the SOCKS/HTTP connector accepts them


@pytest.mark.parametrize(
    "url",
    [
        "https://proxy.example.net:8443",
        "socks5h://proxy.example.net:1080",
        "socks5://proxy.example.net",
        "http://proxy.example.net",
    ],
)
def test_proxy_urls_the_connector_cannot_use_are_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        normalize_proxy_url(url)


def test_proxy_url_is_kept_verbatim_so_saved_records_stay_valid() -> None:
    # Stored account records must survive re-validation unchanged; the
    # connector itself lowercases the scheme.
    assert normalize_proxy_url("SOCKS5://proxy.example.net:1080") == "SOCKS5://proxy.example.net:1080"


# --- startup


def _clear_kwork_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        if name.upper().startswith("KWORK_"):
            monkeypatch.delenv(name, raising=False)


def test_main_runs_stdio_without_banner_or_update_check(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_kwork_environment(monkeypatch)
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    monkeypatch.setenv("KWORK_EXPECTED_USER_ID", "42")
    monkeypatch.setenv("KWORK_STATE_DIR", str(state_dir))
    runs: list[dict[str, Any]] = []
    configs: list[Any] = []

    def fake_create_server(*, config: Any) -> SimpleNamespace:
        configs.append(config)
        return SimpleNamespace(run=lambda **kwargs: runs.append(kwargs))

    monkeypatch.setattr("kwork_mcp.server.create_server", fake_create_server)

    kwork_mcp.main([])

    assert runs == [{"transport": "stdio", "show_banner": False}]
    assert configs[0].expected_user_id == 42


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"KWORK_STATE_DIR": "relative/state"}, "KWORK_STATE_DIR"),
        ({"KWORK_EXPECTED_USER_ID": "not-a-number"}, "KWORK_EXPECTED_USER_ID"),
        ({}, "KWORK_EXPECTED_USER_ID"),
        (
            {
                "KWORK_EXPECTED_USER_ID": "42",
                "KWORK_RETRY_BACKOFF_BASE": "5",
                "KWORK_RETRY_BACKOFF_MAX": "1",
            },
            "retry_backoff_max",
        ),
    ],
)
def test_main_reports_invalid_configuration_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    env: dict[str, str],
    expected: str,
) -> None:
    _clear_kwork_environment(monkeypatch)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        "kwork_mcp.server.create_server",
        lambda **_kwargs: pytest.fail("server must not start with invalid configuration"),
    )

    with pytest.raises(SystemExit) as exited:
        kwork_mcp.main([])

    assert exited.value.code == 2
    captured = capsys.readouterr()
    assert expected in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""


class TTYBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.mark.asyncio
async def test_bootstrap_names_the_prompted_value_that_failed_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_kwork_environment(monkeypatch)
    monkeypatch.setenv("KWORK_EXPECTED_USER_ID", "42")
    monkeypatch.setenv("KWORK_STATE_DIR", str(tmp_path / "state"))
    answers = iter(["login@example.com", "password-value", "", "https://proxy.example.net:8443"])

    code = await run_bootstrap_cli(
        [],
        stdin=TTYBuffer(),
        stdout=io.StringIO(),
        stderr=(stderr := TTYBuffer()),
        getpass_fn=lambda *_args, **_kwargs: next(answers),
        client_factory=lambda _config: pytest.fail("invalid input must fail before network"),  # type: ignore[arg-type,return-value]
        home_dir=tmp_path,
    )

    assert code == 2
    message = stderr.getvalue()
    assert "Proxy URL" in message
    assert "KWORK_EXPECTED_USER_ID" not in message
    assert "proxy.example.net" not in message
