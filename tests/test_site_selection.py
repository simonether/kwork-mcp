"""Locale selection: KWORK_SITE=ru|com derives all Kwork hostnames."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from kwork_mcp.config import KworkConfig
from kwork_mcp.errors import ContractDriftError
from kwork_mcp.upstream import GatewayKworkClient, SecureKworkWebClient, make_client


def test_default_site_is_ru(config_factory: Callable[..., KworkConfig]) -> None:
    config = config_factory()
    assert config.site == "ru"
    assert config.api_host == "https://api.kwork.ru/{}"
    assert config.web_base_url == "https://kwork.ru/"
    assert config.web_login_redirect == "/exchange"


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
