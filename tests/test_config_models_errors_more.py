from __future__ import annotations

import importlib
import importlib.metadata
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
from aiohttp_socks import ProxyError as SocksProxyError
from kwork.exceptions import KworkException, KworkHTTPException, KworkRetryExceeded
from pydantic import ValidationError

import kwork_mcp
from kwork_mcp.config import (
    KworkConfig,
    _default_state_dir,
    contains_unsafe_text_codepoint,
)
from kwork_mcp.errors import (
    AmbiguousWriteError,
    ContractDriftError,
    GatewayError,
    classify_upstream_error,
)
from kwork_mcp.models import (
    ErrorCode,
    ErrorInfo,
    KnowledgeState,
    ResultEnvelope,
    ResultMeta,
    SendMessageRequest,
    WriteAction,
)
from kwork_mcp.server import create_server


def test_default_state_dir_honors_xdg_and_falls_back_to_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/xdg-state")
    assert _default_state_dir() == Path("/tmp/xdg-state/kwork-mcp")
    monkeypatch.delenv("XDG_STATE_HOME")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/tmp/home")))
    assert _default_state_dir() == Path("/tmp/home/.local/state/kwork-mcp")
    assert contains_unsafe_text_codepoint("safe") is False
    assert contains_unsafe_text_codepoint("bad\x7f") is True
    assert contains_unsafe_text_codepoint("bad\u202e") is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"token": "x", "login": "login"},
        {"token": "x", "password": "password"},
        {"token": "x", "login": "bad\nlogin", "password": "password"},
        {"token": "x", "expected_username": "bad\x7fusername"},
        {"token": "x", "phone_last": "123"},
        {"token": "x", "phone_last": "abcd"},
        {"token": "x", "state_dir": Path("relative")},
        {"token": "x", "token_file": "/tmp/legacy"},
        {"token": "x", "retry_backoff_base": 2.0, "retry_backoff_max": 1.0},
        {"token": "x", "proxy_url": "http://proxy.example:not-a-port"},
        {"token": "x", "proxy_url": "http://proxy.example:0"},
        {"token": "x", "proxy_url": "http://proxy.example/path?query=1#fragment"},
    ],
)
def test_configuration_rejects_invalid_auth_and_operational_values(
    tmp_path: Path,
    kwargs: dict[str, Any],
) -> None:
    kwargs.setdefault("state_dir", tmp_path)
    with pytest.raises(ValidationError):
        KworkConfig(**kwargs)


def test_configuration_normalizes_optional_secrets_and_scopes(tmp_path: Path) -> None:
    token = KworkConfig(
        token="token-value",
        phone_last=" ",
        proxy_url=" ",
        state_dir=tmp_path,
    )
    assert token.phone_last_value is None
    assert token.proxy_value is None
    assert token.password_value == ""
    assert token.bootstrap_scope.startswith("unbound-")
    assert token.token_cache_is_bound is False

    username = KworkConfig(
        token="token",
        expected_username=" @Fixture ",
        state_dir=tmp_path,
    )
    assert username.bootstrap_scope != token.bootstrap_scope
    assert username.token_cache_is_bound is True

    login = KworkConfig(
        token=None,
        login="User@Example.Test",
        password="password",
        phone_last="1234",
        proxy_url="HTTPS://alice:secret@proxy.example:443",
        state_dir=tmp_path,
    )
    assert login.phone_last_value == "1234"
    assert login.proxy_value == "HTTPS://alice:secret@proxy.example:443"
    assert {
        "User@Example.Test",
        "password",
        "1234",
        "alice",
        "secret",
    } <= set(login.redaction_secrets)
    assert login.bootstrap_scope.startswith("unbound-")
    assert login.token_cache_is_bound is True
    empty_username = KworkConfig(
        token="token",
        expected_username=" @ ",
        state_dir=tmp_path,
    )
    assert empty_username.expected_username is None


@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (403, {"message": "captcha required"}, ErrorCode.CAPTCHA),
        (403, {"message": "csrf failed"}, ErrorCode.CSRF),
        (403, {"message": "IP address blocked"}, ErrorCode.IP_BLOCKED),
        (403, {"message": "unknown"}, ErrorCode.PERMISSION),
        (400, {"error": "invalid token"}, ErrorCode.AUTH_EXPIRED),
        (400, {"response": "authorization required"}, ErrorCode.AUTH_REQUIRED),
        (400, {"message": "not enough connects"}, ErrorCode.INSUFFICIENT_CONNECTS),
        (400, {"message": "already sent"}, ErrorCode.DUPLICATE),
        (400, {"message": "project closed"}, ErrorCode.CLOSED_PROJECT),
        (400, {"message": "not found"}, ErrorCode.NOT_FOUND),
    ],
)
def test_http_business_error_classification(
    status: int,
    payload: dict[str, Any],
    expected: ErrorCode,
) -> None:
    error = classify_upstream_error(KworkHTTPException("opaque", status=status, response_json=payload))
    assert error.code is expected


def test_rate_limit_retry_after_validation_and_gateway_passthrough() -> None:
    numeric = classify_upstream_error(
        KworkHTTPException(
            "limited",
            status=429,
            response_json={"retryAfter": 2.5},
        )
    )
    assert numeric.retry_after_seconds == 2.5
    invalid = classify_upstream_error(
        KworkHTTPException(
            "limited",
            status=429,
            response_json={"retry_after": -1},
        )
    )
    assert invalid.retry_after_seconds is None

    original = GatewayError(ErrorCode.CIRCUIT_OPEN)
    assert classify_upstream_error(original) is original
    request_timeout = classify_upstream_error(KworkHTTPException("timeout", status=408))
    assert request_timeout.code is ErrorCode.TIMEOUT
    assert request_timeout.safe_to_retry is True


def test_retry_exhausted_without_last_error_and_os_error() -> None:
    exhausted = KworkRetryExceeded("failed", attempts=2, last_error=None)
    assert classify_upstream_error(exhausted).code is ErrorCode.INTERNAL
    assert classify_upstream_error(OSError("socket")).code is ErrorCode.UPSTREAM_UNAVAILABLE


def test_proxy_exceptions_and_rate_limit_business_exception() -> None:
    connection_key = aiohttp.client_reqrep.ConnectionKey(
        host="proxy",
        port=8080,
        is_ssl=False,
        ssl=True,
        proxy=None,
        proxy_auth=None,
        proxy_headers_hash=None,
    )
    proxy_error = aiohttp.ClientProxyConnectionError(
        connection_key,
        OSError("proxy down"),
    )
    assert classify_upstream_error(proxy_error).code is ErrorCode.PROXY
    assert classify_upstream_error(SocksProxyError("proxy")).code is ErrorCode.PROXY
    business = classify_upstream_error(KworkException("rate limit"))
    assert business.code is ErrorCode.RATE_LIMIT
    assert business.retryable and business.safe_to_retry


def test_specialized_errors_have_safe_semantics() -> None:
    contract = ContractDriftError("signature")
    assert contract.code is ErrorCode.CONTRACT_DRIFT
    assert contract.diagnostic == "signature"
    ambiguous = AmbiguousWriteError("timeout")
    assert ambiguous.code is ErrorCode.AMBIGUOUS_WRITE
    assert ambiguous.reconciliation_required is True
    assert ambiguous.retryable is False


def test_result_envelope_rejects_error_on_success_and_accepts_typed_error() -> None:
    meta = ResultMeta(
        observed_at="2026-07-27T00:00:00Z",  # type: ignore[arg-type]
        correlation_id="correlation",
    )
    error = ErrorInfo(
        code=ErrorCode.PERMISSION,
        message="safe",
        correlation_id="correlation",
    )
    with pytest.raises(ValidationError, match="successful knowledge state"):
        ResultEnvelope[dict[str, Any]](
            knowledge_state=KnowledgeState.KNOWN_EMPTY,
            summary="invalid",
            error=error,
            meta=meta,
        )
    result = ResultEnvelope[dict[str, Any]](
        knowledge_state=KnowledgeState.UNKNOWN_ERROR,
        summary="failed",
        error=error,
        meta=meta,
    )
    assert result.error is error


@pytest.mark.parametrize(
    "kwargs",
    [
        {"user_id": None, "username": None},
        {"user_id": 42, "username": "fixture"},
    ],
)
def test_send_message_requires_exactly_one_recipient(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        SendMessageRequest(
            action=WriteAction.SEND_MESSAGE,
            text="hello",
            **kwargs,
        )
    assert (
        SendMessageRequest(
            action=WriteAction.SEND_MESSAGE,
            user_id=42,
            text="hello",
        ).user_id
        == 42
    )


def test_package_main_builds_and_runs_stdio_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_server = SimpleNamespace(run=lambda **kwargs: calls.append(kwargs))
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "kwork_mcp.server.create_server",
        lambda: fake_server,
    )
    monkeypatch.setattr(
        "kwork_mcp.secret_server_environment_present",
        lambda: False,
    )
    kwork_mcp.main([])
    assert calls == [{"transport": "stdio"}]
    assert importlib.import_module("kwork_mcp.__main__").main is kwork_mcp.main


def test_package_main_rejects_secret_env_and_argv_without_reflection(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "server-env-secret-sentinel"
    monkeypatch.setenv("KWORK_PASSWORD", secret)
    monkeypatch.setattr(
        "kwork_mcp.server.create_server",
        lambda: pytest.fail("server construction must not run"),
    )
    with pytest.raises(SystemExit) as env_exit:
        kwork_mcp.main([])
    assert env_exit.value.code == 2
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err

    argv_secret = "server-argv-secret-sentinel"
    with pytest.raises(SystemExit) as argv_exit:
        kwork_mcp.main([f"--token={argv_secret}"])
    assert argv_exit.value.code == 2
    captured = capsys.readouterr()
    assert argv_secret not in captured.out + captured.err


@pytest.mark.parametrize(
    "secret_config",
    [
        {"token": "factory-token-sentinel"},
        {
            "login": "factory-login-sentinel",
            "password": "factory-password-sentinel",
        },
        {
            "token": "factory-token-with-phone",
            "phone_last": "1234",
        },
        {
            "token": "factory-token-with-proxy",
            "proxy_url": "socks5://proxy-user:proxy-pass@localhost:1080",
        },
    ],
)
def test_public_server_factory_rejects_every_bootstrap_credential_source(
    tmp_path: Path,
    secret_config: dict[str, Any],
) -> None:
    config = KworkConfig(
        state_dir=tmp_path,
        expected_user_id=42,
        persist_token=True,
        **secret_config,
    )
    with pytest.raises(ValueError) as caught:
        create_server(config=config)
    rendered = str(caught.value)
    for sentinel in (
        "factory-token-sentinel",
        "factory-login-sentinel",
        "factory-password-sentinel",
        "factory-token-with-phone",
        "factory-token-with-proxy",
        "proxy-user",
        "proxy-pass",
        "1234",
    ):
        assert sentinel not in rendered


def test_contract_distribution_missing_maps_to_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kwork_mcp import contracts

    def missing(_name: str) -> str:
        raise importlib.metadata.PackageNotFoundError

    monkeypatch.setattr(importlib.metadata, "version", missing)
    with pytest.raises(ContractDriftError) as caught:
        contracts.verify_upstream_contract()
    assert caught.value.diagnostic == "kwork_distribution_missing"


def test_contract_version_mismatch_maps_to_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kwork_mcp import contracts

    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "9.9.9")
    with pytest.raises(ContractDriftError) as caught:
        contracts.verify_upstream_contract()
    assert caught.value.diagnostic == "kwork_version:9.9.9"


@pytest.mark.parametrize(
    ("kind", "diagnostic"),
    [
        ("method", "missing_method:missing"),
        ("generic", "generic_signature:missing"),
        ("web", "web_signature:missing"),
    ],
)
def test_contract_missing_methods_fail_loud(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    diagnostic: str,
) -> None:
    from kwork_mcp import contracts

    monkeypatch.setattr(contracts.importlib.metadata, "version", lambda _name: "0.2.0")
    monkeypatch.setattr(contracts, "_EXPECTED_SIGNATURES", {})
    monkeypatch.setattr(contracts, "_GENERIC_METHODS", set())
    monkeypatch.setattr(contracts, "_EXPECTED_WEB_SIGNATURES", {})
    if kind == "method":
        monkeypatch.setattr(contracts, "_EXPECTED_SIGNATURES", {"missing": ()})
    elif kind == "generic":
        monkeypatch.setattr(contracts, "_GENERIC_METHODS", {"missing"})
    else:
        monkeypatch.setattr(contracts, "_EXPECTED_WEB_SIGNATURES", {"missing": ()})
    with pytest.raises(ContractDriftError) as caught:
        contracts.verify_upstream_contract()
    assert caught.value.diagnostic == diagnostic


def test_contract_required_model_fields_fail_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kwork_mcp import contracts

    monkeypatch.setattr(contracts, "_EXPECTED_SIGNATURES", {})
    monkeypatch.setattr(contracts, "_GENERIC_METHODS", set())
    monkeypatch.setattr(contracts, "_EXPECTED_WEB_SIGNATURES", {})
    monkeypatch.setattr(contracts, "Actor", SimpleNamespace(model_fields={}))
    with pytest.raises(ContractDriftError) as actor:
        contracts.verify_upstream_contract()
    assert actor.value.diagnostic == "actor_model_fields"

    monkeypatch.setattr(
        contracts,
        "Actor",
        SimpleNamespace(model_fields={"id": None, "username": None}),
    )
    monkeypatch.setattr(contracts, "WantWorker", SimpleNamespace(model_fields={}))
    with pytest.raises(ContractDriftError) as project:
        contracts.verify_upstream_contract()
    assert project.value.diagnostic == "project_model_fields"


def test_contract_private_client_and_fingerprint_fail_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kwork_mcp import contracts

    monkeypatch.setattr(contracts, "_EXPECTED_SIGNATURES", {})
    monkeypatch.setattr(contracts, "_GENERIC_METHODS", set())
    monkeypatch.setattr(contracts, "_EXPECTED_WEB_SIGNATURES", {})

    class EmptyClient:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

    monkeypatch.setattr(contracts, "Kwork", EmptyClient)
    with pytest.raises(ContractDriftError) as private:
        contracts.verify_upstream_contract()
    assert private.value.diagnostic == "private_client_dependencies"

    from kwork import Kwork

    monkeypatch.setattr(contracts, "Kwork", Kwork)
    monkeypatch.setattr(contracts, "signature_fingerprint", lambda: "bad")
    with pytest.raises(ContractDriftError) as fingerprint:
        contracts.verify_upstream_contract()
    assert fingerprint.value.diagnostic == "fingerprint:bad"


def test_contract_signature_and_private_web_dependencies_fail_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kwork_mcp import contracts

    monkeypatch.setattr(
        contracts,
        "_EXPECTED_SIGNATURES",
        {"get_token": (("unexpected", "POSITIONAL_ONLY", "<required>"),)},
    )
    monkeypatch.setattr(contracts, "_GENERIC_METHODS", set())
    monkeypatch.setattr(contracts, "_EXPECTED_WEB_SIGNATURES", {})
    with pytest.raises(ContractDriftError) as signature:
        contracts.verify_upstream_contract()
    assert (signature.value.diagnostic or "").startswith("signature:get_token:")

    from kwork import Kwork

    class WrongWebClient:
        def __init__(self, _client: Kwork) -> None:
            self._api = object()
            self.base_url = "https://evil.example/"

    monkeypatch.setattr(contracts, "_EXPECTED_SIGNATURES", {})
    monkeypatch.setattr(contracts, "KworkWebClient", WrongWebClient)
    with pytest.raises(ContractDriftError) as private_web:
        contracts.verify_upstream_contract()
    assert private_web.value.diagnostic == "private_web_client_dependencies"
