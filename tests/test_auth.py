"""Tests for the OAuth2 client_credentials flow + token cache."""

from __future__ import annotations

import base64
import json
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from ebay_mcp.auth import (
    TOKEN_REFRESH_BUFFER_SECONDS,
    _token_cache_path,
    clear_token_cache,
    get_app_token,
)
from ebay_mcp.config import load_config


def _config_file(tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(
        textwrap.dedent(
            """
            [hosts.sandbox]
            app_id = "TheApp"
            dev_id = "TheDev"
            cert_id = "TheCert"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    return p


def _redirect_cache_home(monkeypatch, tmp_path: Path) -> Path:
    """Point Path.home() at a temp dir so token cache writes go there."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path / ".config" / "ebay-mcp"


@respx.mock
def test_get_app_token_fetches_and_caches(monkeypatch, tmp_path):
    cfg = load_config(_config_file(tmp_path))
    _redirect_cache_home(monkeypatch, tmp_path)

    route = respx.post("https://api.sandbox.ebay.com/identity/v1/oauth2/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "AAA-token",
                "token_type": "Bearer",
                "expires_in": 7200,
                "scope": "https://api.ebay.com/oauth/api_scope",
            },
        )
    )

    token = get_app_token(cfg, "sandbox")
    assert token == "AAA-token"
    assert route.called

    # Cache file written with the right shape.
    cache_file = _token_cache_path("sandbox")
    assert cache_file.exists()
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    assert data["client_credentials"]["access_token"] == "AAA-token"
    assert "expires_at" in data["client_credentials"]


@respx.mock
def test_get_app_token_reuses_fresh_cache(monkeypatch, tmp_path):
    cfg = load_config(_config_file(tmp_path))
    _redirect_cache_home(monkeypatch, tmp_path)

    # Pre-populate cache with a non-expired token.
    cache_file = _token_cache_path("sandbox")
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    expires_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    cache_file.write_text(
        json.dumps({"client_credentials": {"access_token": "CACHED", "expires_at": expires_at}}),
        encoding="utf-8",
    )

    route = respx.post("https://api.sandbox.ebay.com/identity/v1/oauth2/token").mock(
        return_value=httpx.Response(200, json={"access_token": "FRESH", "expires_in": 7200})
    )

    token = get_app_token(cfg, "sandbox")
    assert token == "CACHED"
    assert not route.called  # MUST NOT have hit the network


@respx.mock
def test_get_app_token_refreshes_near_expiry(monkeypatch, tmp_path):
    cfg = load_config(_config_file(tmp_path))
    _redirect_cache_home(monkeypatch, tmp_path)

    # Cache expires inside the refresh buffer → should refresh.
    cache_file = _token_cache_path("sandbox")
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    expires_at = (
        datetime.now(UTC) + timedelta(seconds=TOKEN_REFRESH_BUFFER_SECONDS - 10)
    ).isoformat()
    cache_file.write_text(
        json.dumps({"client_credentials": {"access_token": "STALE", "expires_at": expires_at}}),
        encoding="utf-8",
    )

    route = respx.post("https://api.sandbox.ebay.com/identity/v1/oauth2/token").mock(
        return_value=httpx.Response(200, json={"access_token": "REFRESHED", "expires_in": 7200})
    )

    token = get_app_token(cfg, "sandbox")
    assert token == "REFRESHED"
    assert route.called


@respx.mock
def test_get_app_token_sends_basic_auth(monkeypatch, tmp_path):
    cfg = load_config(_config_file(tmp_path))
    _redirect_cache_home(monkeypatch, tmp_path)

    route = respx.post("https://api.sandbox.ebay.com/identity/v1/oauth2/token").mock(
        return_value=httpx.Response(200, json={"access_token": "x", "expires_in": 7200})
    )

    get_app_token(cfg, "sandbox")

    assert route.called
    req = route.calls.last.request
    auth_header = req.headers.get("Authorization", "")
    assert auth_header.startswith("Basic ")
    decoded = base64.b64decode(auth_header.split(" ", 1)[1]).decode()
    assert decoded == "TheApp:TheCert"
    # And it's a client_credentials grant.
    body = req.content.decode()
    assert "grant_type=client_credentials" in body
    assert "scope=" in body


@respx.mock
def test_get_app_token_handles_corrupt_cache(monkeypatch, tmp_path):
    cfg = load_config(_config_file(tmp_path))
    _redirect_cache_home(monkeypatch, tmp_path)

    cache_file = _token_cache_path("sandbox")
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text("not valid json {", encoding="utf-8")

    route = respx.post("https://api.sandbox.ebay.com/identity/v1/oauth2/token").mock(
        return_value=httpx.Response(200, json={"access_token": "FROM_FETCH", "expires_in": 7200})
    )

    token = get_app_token(cfg, "sandbox")
    assert token == "FROM_FETCH"
    assert route.called


@respx.mock
def test_get_app_token_propagates_401(monkeypatch, tmp_path):
    cfg = load_config(_config_file(tmp_path))
    _redirect_cache_home(monkeypatch, tmp_path)

    respx.post("https://api.sandbox.ebay.com/identity/v1/oauth2/token").mock(
        return_value=httpx.Response(401, json={"error": "invalid_client"})
    )

    with pytest.raises(httpx.HTTPStatusError):
        get_app_token(cfg, "sandbox")


def test_clear_token_cache_removes_file(monkeypatch, tmp_path):
    _redirect_cache_home(monkeypatch, tmp_path)
    p = _token_cache_path("sandbox")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}", encoding="utf-8")
    assert p.exists()

    clear_token_cache("sandbox")
    assert not p.exists()


def test_clear_token_cache_idempotent_when_absent(monkeypatch, tmp_path):
    _redirect_cache_home(monkeypatch, tmp_path)
    # Should not raise even if no cache file exists.
    clear_token_cache("sandbox")


@respx.mock
def test_get_app_token_uses_production_url(monkeypatch, tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        textwrap.dedent(
            """
            default_host = "production"

            [hosts.production]
            app_id = "PrdApp"
            dev_id = "PrdDev"
            cert_id = "PrdCert"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    cfg = load_config(p)
    _redirect_cache_home(monkeypatch, tmp_path)

    sandbox_route = respx.post("https://api.sandbox.ebay.com/identity/v1/oauth2/token")
    prod_route = respx.post("https://api.ebay.com/identity/v1/oauth2/token").mock(
        return_value=httpx.Response(200, json={"access_token": "PRD", "expires_in": 7200})
    )

    token = get_app_token(cfg, "production")
    assert token == "PRD"
    assert prod_route.called
    assert not sandbox_route.called
