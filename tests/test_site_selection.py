"""Locale selection: KWORK_SITE=ru|com derives all Kwork hostnames."""

from __future__ import annotations

import io
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from kwork_mcp.bootstrap import run_bootstrap_cli
from kwork_mcp.config import KworkConfig
from kwork_mcp.errors import ContractDriftError, GatewayError
from kwork_mcp.models import ErrorCode, KworkRecord, SetKworkStateRequest, WriteAction, WriteState
from kwork_mcp.upstream import GatewayKworkClient, SecureKworkWebClient, make_client
from tests.test_bootstrap_discovery import _environment, _run
from tests.test_write_safety import (
    SCOPE,
    SafetyGateway,
    SafetySession,
    TimeoutGateway,
    TTYBuffer,
    _offer,
    _operator_environment,
    _prepare_and_commit,
    _record,
    _writes_config,
)


def test_default_site_is_ru(config_factory: Callable[..., KworkConfig]) -> None:
    config = config_factory()
    assert config.site == "ru"
    assert config.api_host == "https://api.kwork.ru/{}"
    assert config.web_base_url == "https://kwork.ru/"
    assert config.web_login_redirect == "/projects"


def test_com_site_derives_com_hosts(config_factory: Callable[..., KworkConfig]) -> None:
    config = config_factory(site="com")
    assert config.api_host == "https://api.kwork.com/{}"
    assert config.web_base_url == "https://kwork.com/"
    assert config.web_login_redirect == "/"


def test_site_rejects_unknown_locale(config_factory: Callable[..., KworkConfig]) -> None:
    with pytest.raises(ValueError):
        config_factory(site="org")


def test_make_client_uses_configured_hosts(config_factory: Callable[..., KworkConfig]) -> None:
    client = make_client(config_factory(site="com"))
    assert client._api_host == "https://api.kwork.com/{}"
    web = client.web
    assert isinstance(web, SecureKworkWebClient)
    assert web.base_url == "https://kwork.com/"


def test_com_web_client_accepts_com_and_rejects_ru() -> None:
    web = GatewayKworkClient("", "", web_base_url="https://kwork.com/").web
    web._validate_kwork_url("https://kwork.com/some/path")
    web._validate_kwork_url("https://www.kwork.com/some/path")
    with pytest.raises(ContractDriftError) as caught:
        web._validate_kwork_url("https://kwork.ru/some/path")
    assert caught.value.diagnostic == "web_login_url_untrusted_host"


def test_ru_web_client_rejects_com() -> None:
    web = GatewayKworkClient("", "").web
    with pytest.raises(ContractDriftError) as caught:
        web._validate_kwork_url("https://kwork.com/some/path")
    assert caught.value.diagnostic == "web_login_url_untrusted_host"


# --- kwork.com has no project exchange: refuse locally, never spend a request


class CountingSession(SafetySession):
    def __init__(self) -> None:
        super().__init__()
        self.reads: list[str] = []

    async def call_read(self, route: str, operation: Callable[[Any], Awaitable[Any]]) -> Any:
        self.reads.append(route)
        return await super().call_read(route, operation)


def _on_site(config: KworkConfig, site: str) -> KworkConfig:
    return KworkConfig(**{**config.model_dump(), "site": site})


@pytest.mark.asyncio
async def test_com_refuses_exchange_reads_without_a_request(config_factory: Callable[..., KworkConfig]) -> None:
    session = CountingSession()
    gateway = SafetyGateway(config_factory(site="com"), session)

    with pytest.raises(GatewayError) as projects:
        await gateway.discover_projects(
            mode="all",
            category_ids=None,
            price_from=None,
            price_to=None,
            hiring_from=None,
            offers_from=None,
            offers_to=None,
            query=None,
            cursor=None,
        )
    with pytest.raises(GatewayError) as favorites:
        await gateway.list_favorite_categories()

    assert projects.value.code is ErrorCode.SITE_UNSUPPORTED
    assert favorites.value.code is ErrorCode.SITE_UNSUPPORTED
    assert session.reads == []


@pytest.mark.asyncio
async def test_account_status_reports_the_site(config_factory: Callable[..., KworkConfig]) -> None:
    assert (await SafetyGateway(config_factory()).account_status()).site == "ru"
    assert (await SafetyGateway(config_factory(site="com")).account_status()).site == "com"


@pytest.mark.asyncio
async def test_com_refuses_to_prepare_an_offer(config_factory: Callable[..., KworkConfig]) -> None:
    gateway = SafetyGateway(_on_site(_writes_config(config_factory), "com"))

    with pytest.raises(GatewayError) as refused:
        await gateway.prepare_write(_offer(), "com-offer", correlation_id="prepare")

    assert refused.value.code is ErrorCode.SITE_UNSUPPORTED
    assert await gateway.coordinator.get_write_by_idempotency(scope=SCOPE, idempotency_key="com-offer") is None


# --- a write commits and reconciles only on the site it was prepared for


@pytest.mark.asyncio
async def test_write_records_the_site_it_was_prepared_for(config_factory: Callable[..., KworkConfig]) -> None:
    ru_config = _writes_config(config_factory)
    gateways = {site: SafetyGateway(_on_site(ru_config, site)) for site in ("ru", "com")}
    request = SetKworkStateRequest(action=WriteAction.SET_KWORK_STATE, kwork_id=7, target_state="active")
    for gateway in gateways.values():
        gateway.kworks = [KworkRecord(kwork_id=7, status_group_id=3, status_group_name="Остановленные", raw={"id": 7})]

    prepared = await gateways["com"].prepare_write(request, "com-kwork-state", correlation_id="prepare")
    with pytest.raises(GatewayError) as reprepared:
        await gateways["ru"].prepare_write(request, "com-kwork-state", correlation_id="ru-prepare")

    record = await gateways["com"].coordinator.get_write(prepared.write_id, scope=SCOPE)
    assert record is not None
    assert json.loads(record.payload_json)["prepared_site"] == "com"
    assert reprepared.value.code is ErrorCode.SITE_MISMATCH


@pytest.mark.asyncio
async def test_write_prepared_on_ru_is_not_committed_on_com(config_factory: Callable[..., KworkConfig]) -> None:
    ru_config = _writes_config(config_factory)
    ru_session = SafetySession()
    ru = SafetyGateway(ru_config, ru_session)
    com_session = SafetySession()
    com = SafetyGateway(_on_site(ru_config, "com"), com_session)
    prepared = await ru.prepare_write(_offer(), "ru-offer", correlation_id="prepare")
    assert prepared.confirmation_token is not None
    commit = {
        "write_id": prepared.write_id,
        "payload_hash": prepared.payload_hash,
        "confirmation_token": prepared.confirmation_token,
    }

    with pytest.raises(GatewayError) as refused:
        await com.commit_write(**commit, correlation_id="com-commit")
    with pytest.raises(GatewayError) as reprepared:
        await com.prepare_write(_offer(), "ru-offer", correlation_id="com-prepare")

    assert refused.value.code is ErrorCode.SITE_MISMATCH
    assert reprepared.value.code is ErrorCode.SITE_UNSUPPORTED
    assert com_session.remote_steps == []
    status = await ru.get_write_status(prepared.write_id, correlation_id="status")
    assert status is not None
    assert status.state is WriteState.PREPARED
    committed = await ru.commit_write(**commit, correlation_id="ru-commit")
    assert committed.state is WriteState.SUCCEEDED


@pytest.mark.asyncio
async def test_unknown_write_is_reconciled_only_on_its_own_site(config_factory: Callable[..., KworkConfig]) -> None:
    ru_config = _writes_config(config_factory)
    unknown = await _prepare_and_commit(TimeoutGateway(ru_config), _offer(), "ru-unknown")
    assert unknown.state is WriteState.SUBMISSION_UNKNOWN

    with pytest.raises(GatewayError) as refused:
        await TimeoutGateway(_on_site(ru_config, "com")).reconcile_write(unknown.write_id, correlation_id="reconcile")

    assert refused.value.code is ErrorCode.SITE_MISMATCH
    status = await TimeoutGateway(ru_config).get_write_status(unknown.write_id, correlation_id="status")
    assert status is not None
    assert status.state is WriteState.SUBMISSION_UNKNOWN


@pytest.mark.asyncio
async def test_operator_commands_name_the_site_of_a_write(
    config_factory: Callable[..., KworkConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    com_config = _on_site(_writes_config(config_factory), "com")
    gateway = TimeoutGateway(com_config)
    gateway.kworks = [KworkRecord(kwork_id=7, status_group_id=3, status_group_name="Остановленные", raw={"id": 7})]
    request = SetKworkStateRequest(action=WriteAction.SET_KWORK_STATE, kwork_id=7, target_state="active")
    unknown = await _prepare_and_commit(gateway, request, "com-unknown")
    assert unknown.state is WriteState.SUBMISSION_UNKNOWN
    # The operator runs with the default site; the record still says where to look.
    _operator_environment(monkeypatch, com_config)
    monkeypatch.delenv("KWORK_SITE", raising=False)

    code = await run_bootstrap_cli(
        ["pending-writes"], stdin=TTYBuffer(), stdout=(listed := io.StringIO()), stderr=TTYBuffer()
    )
    assert code == 0
    assert json.loads(listed.getvalue())["writes"][0]["site"] == "com"

    code = await run_bootstrap_cli(
        ["resolve-write", unknown.write_id, "absent"],
        stdin=TTYBuffer("нет\n"),
        stdout=io.StringIO(),
        stderr=(prompt := TTYBuffer()),
    )
    assert code == 1
    assert "Убедитесь на kwork.com" in prompt.getvalue()


@pytest.mark.parametrize(("site", "accepted"), [("ru", True), ("com", False)])
def test_writes_prepared_before_site_selection_belong_to_ru(
    config_factory: Callable[..., KworkConfig],
    site: str,
    accepted: bool,
) -> None:
    gateway = SafetyGateway(config_factory(site=site))
    legacy = _record(WriteAction.SEND_MESSAGE)

    if accepted:
        gateway._require_prepared_site(legacy)
    else:
        with pytest.raises(GatewayError) as refused:
            gateway._require_prepared_site(legacy)
        assert refused.value.code is ErrorCode.SITE_MISMATCH


# --- bootstrap prints KWORK_SITE only when it is not the default


@pytest.mark.asyncio
@pytest.mark.parametrize("site", [None, "com"])
async def test_bootstrap_environment_carries_a_non_default_site(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    site: str | None,
) -> None:
    _environment(monkeypatch, tmp_path / "state")
    if site is not None:
        monkeypatch.setenv("KWORK_SITE", site)
    configs: list[KworkConfig] = []

    code, stdout, _stderr = await _run(tmp_path, confirmation="да\n", clients=[], configs=configs)

    assert code == 0
    if site is None:
        assert "KWORK_SITE" not in stdout
        assert "claude mcp add kwork --scope user " in stdout
        assert configs[-1].api_host == "https://api.kwork.ru/{}"
    else:
        assert f"claude mcp add kwork-{site} --scope user -e KWORK_SITE={site} " in stdout
        assert f"codex mcp add kwork-{site} --env KWORK_SITE={site} " in stdout
        assert all(config.api_host == "https://api.kwork.com/{}" for config in configs)
