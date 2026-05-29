"""Tests for the OAuth2 authorization_code (user) flow."""

from __future__ import annotations

import json
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from ebay_mcp.auth import (
    USER_TOKEN_SCOPES,
    UserNotAuthenticated,
    _token_cache_path,
    complete_user_auth,
    get_user_token,
    start_user_auth,
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
            redirect_uri = "https://localhost/oauth/callback"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    return p


def _redirect_home(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


def _read_cache(host: str) -> dict:
    p = _token_cache_path(host)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def _write_cache(host: str, data: dict) -> None:
    p = _token_cache_path(host)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")


# --------------------- start_user_auth --------------------------------------


def test_start_user_auth_returns_url_with_params(monkeypatch, tmp_path):
    cfg = load_config(_config_file(tmp_path))
    _redirect_home(monkeypatch, tmp_path)

    result = start_user_auth(cfg, "sandbox")

    assert result["host"] == "sandbox"
    assert "auth_url" in result
    assert "state" in result
    assert len(result["state"]) >= 20  # secrets.token_urlsafe(24) gives ~32 chars

    parsed = urlparse(result["auth_url"])
    assert parsed.netloc == "auth.sandbox.ebay.com"
    assert parsed.path == "/oauth2/authorize"
    qs = parse_qs(parsed.query)
    assert qs["client_id"] == ["TheApp"]
    assert qs["response_type"] == ["code"]
    assert qs["redirect_uri"] == ["https://localhost/oauth/callback"]
    assert qs["state"] == [result["state"]]
    # Scopes are space-separated in the value.
    assert all(scope in qs["scope"][0] for scope in USER_TOKEN_SCOPES)


def test_start_user_auth_persists_state_in_cache(monkeypatch, tmp_path):
    cfg = load_config(_config_file(tmp_path))
    _redirect_home(monkeypatch, tmp_path)

    result = start_user_auth(cfg, "sandbox")
    cache = _read_cache("sandbox")

    assert cache["pending_user_auth"]["state"] == result["state"]
    assert "started_at" in cache["pending_user_auth"]


def test_start_user_auth_uses_production_authorize_url(monkeypatch, tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        textwrap.dedent(
            """
            [hosts.production]
            app_id = "PrdApp"
            dev_id = "PrdDev"
            cert_id = "PrdCert"
            redirect_uri = "https://example.com/callback"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    cfg = load_config(p)
    _redirect_home(monkeypatch, tmp_path)

    result = start_user_auth(cfg, "production")
    parsed = urlparse(result["auth_url"])
    assert parsed.netloc == "auth.ebay.com"  # not auth.sandbox.ebay.com


# --------------------- complete_user_auth -----------------------------------


@respx.mock
def test_complete_user_auth_happy_path(monkeypatch, tmp_path):
    cfg = load_config(_config_file(tmp_path))
    _redirect_home(monkeypatch, tmp_path)

    route = respx.post("https://api.sandbox.ebay.com/identity/v1/oauth2/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "USER_ACCESS",
                "refresh_token": "USER_REFRESH",
                "token_type": "User Access Token",
                "expires_in": 7200,
                "refresh_token_expires_in": 47_304_000,
                "scope": " ".join(USER_TOKEN_SCOPES),
            },
        )
    )

    result = complete_user_auth(cfg, "sandbox", code="auth_code_xyz")

    assert result["host"] == "sandbox"
    assert result["authenticated"] is True
    assert "access_expires_at" in result
    assert "refresh_expires_at" in result
    assert route.called

    # Body of the exchange request.
    body = route.calls.last.request.content.decode()
    assert "grant_type=authorization_code" in body
    assert "code=auth_code_xyz" in body
    assert "redirect_uri=" in body

    # Cache persisted.
    cache = _read_cache("sandbox")
    assert cache["user"]["access_token"] == "USER_ACCESS"
    assert cache["user"]["refresh_token"] == "USER_REFRESH"


@respx.mock
def test_complete_user_auth_clears_pending_state(monkeypatch, tmp_path):
    cfg = load_config(_config_file(tmp_path))
    _redirect_home(monkeypatch, tmp_path)
    _write_cache(
        "sandbox",
        {"pending_user_auth": {"state": "pending_state", "started_at": "x"}},
    )

    respx.post("https://api.sandbox.ebay.com/identity/v1/oauth2/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "X",
                "refresh_token": "Y",
                "expires_in": 7200,
                "refresh_token_expires_in": 47_304_000,
            },
        )
    )

    complete_user_auth(cfg, "sandbox", code="c", state="pending_state")
    cache = _read_cache("sandbox")
    assert "pending_user_auth" not in cache


@respx.mock
def test_complete_user_auth_refuses_on_state_mismatch(monkeypatch, tmp_path):
    cfg = load_config(_config_file(tmp_path))
    _redirect_home(monkeypatch, tmp_path)
    _write_cache(
        "sandbox",
        {"pending_user_auth": {"state": "correct_state", "started_at": "x"}},
    )

    # Should NOT hit the token endpoint.
    route = respx.post("https://api.sandbox.ebay.com/identity/v1/oauth2/token")

    result = complete_user_auth(cfg, "sandbox", code="c", state="wrong_state")
    assert result["refused"] is True
    assert result["reason"] == "state_mismatch"
    assert not route.called


@respx.mock
def test_complete_user_auth_omitting_state_skips_check(monkeypatch, tmp_path):
    """state=None is allowed; CSRF check is optional."""
    cfg = load_config(_config_file(tmp_path))
    _redirect_home(monkeypatch, tmp_path)
    _write_cache(
        "sandbox",
        {"pending_user_auth": {"state": "anything", "started_at": "x"}},
    )

    respx.post("https://api.sandbox.ebay.com/identity/v1/oauth2/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "X",
                "refresh_token": "Y",
                "expires_in": 7200,
                "refresh_token_expires_in": 47_304_000,
            },
        )
    )

    result = complete_user_auth(cfg, "sandbox", code="c", state=None)
    assert result["authenticated"] is True


# --------------------- get_user_token ---------------------------------------


def test_get_user_token_no_cache_raises(monkeypatch, tmp_path):
    cfg = load_config(_config_file(tmp_path))
    _redirect_home(monkeypatch, tmp_path)

    with pytest.raises(UserNotAuthenticated, match="no user token cached"):
        get_user_token(cfg, "sandbox")


def test_get_user_token_returns_fresh_cached(monkeypatch, tmp_path):
    cfg = load_config(_config_file(tmp_path))
    _redirect_home(monkeypatch, tmp_path)
    _write_cache(
        "sandbox",
        {
            "user": {
                "access_token": "FRESH",
                "refresh_token": "R",
                "access_expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                "refresh_expires_at": (datetime.now(UTC) + timedelta(days=300)).isoformat(),
            }
        },
    )
    assert get_user_token(cfg, "sandbox") == "FRESH"


@respx.mock
def test_get_user_token_refreshes_when_near_expiry(monkeypatch, tmp_path):
    cfg = load_config(_config_file(tmp_path))
    _redirect_home(monkeypatch, tmp_path)
    _write_cache(
        "sandbox",
        {
            "user": {
                "access_token": "STALE",
                "refresh_token": "STILL_GOOD",
                "access_expires_at": (datetime.now(UTC) + timedelta(seconds=60)).isoformat(),
                "refresh_expires_at": (datetime.now(UTC) + timedelta(days=300)).isoformat(),
                "scope": " ".join(USER_TOKEN_SCOPES),
            }
        },
    )

    route = respx.post("https://api.sandbox.ebay.com/identity/v1/oauth2/token").mock(
        return_value=httpx.Response(200, json={"access_token": "REFRESHED", "expires_in": 7200})
    )

    assert get_user_token(cfg, "sandbox") == "REFRESHED"
    body = route.calls.last.request.content.decode()
    assert "grant_type=refresh_token" in body
    assert "refresh_token=STILL_GOOD" in body

    # Cache updated.
    cache = _read_cache("sandbox")
    assert cache["user"]["access_token"] == "REFRESHED"
    # refresh_token unchanged because eBay didn't rotate it.
    assert cache["user"]["refresh_token"] == "STILL_GOOD"


@respx.mock
def test_get_user_token_rotates_refresh_when_returned(monkeypatch, tmp_path):
    """When eBay returns a new refresh_token, persist it."""
    cfg = load_config(_config_file(tmp_path))
    _redirect_home(monkeypatch, tmp_path)
    _write_cache(
        "sandbox",
        {
            "user": {
                "access_token": "STALE",
                "refresh_token": "OLD_REFRESH",
                "access_expires_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
                "refresh_expires_at": (datetime.now(UTC) + timedelta(days=300)).isoformat(),
            }
        },
    )

    respx.post("https://api.sandbox.ebay.com/identity/v1/oauth2/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "NEW_ACCESS",
                "refresh_token": "NEW_REFRESH",
                "expires_in": 7200,
            },
        )
    )

    get_user_token(cfg, "sandbox")
    cache = _read_cache("sandbox")
    assert cache["user"]["refresh_token"] == "NEW_REFRESH"


def test_get_user_token_expired_refresh_raises(monkeypatch, tmp_path):
    cfg = load_config(_config_file(tmp_path))
    _redirect_home(monkeypatch, tmp_path)
    _write_cache(
        "sandbox",
        {
            "user": {
                "access_token": "STALE",
                "refresh_token": "EXPIRED",
                "access_expires_at": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
                "refresh_expires_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
            }
        },
    )

    with pytest.raises(UserNotAuthenticated, match="expired"):
        get_user_token(cfg, "sandbox")


@respx.mock
def test_get_user_token_propagates_401_on_refresh(monkeypatch, tmp_path):
    cfg = load_config(_config_file(tmp_path))
    _redirect_home(monkeypatch, tmp_path)
    _write_cache(
        "sandbox",
        {
            "user": {
                "access_token": "STALE",
                "refresh_token": "BAD",
                "access_expires_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
                "refresh_expires_at": (datetime.now(UTC) + timedelta(days=300)).isoformat(),
            }
        },
    )

    respx.post("https://api.sandbox.ebay.com/identity/v1/oauth2/token").mock(
        return_value=httpx.Response(401, json={"error": "invalid_grant"})
    )

    with pytest.raises(httpx.HTTPStatusError):
        get_user_token(cfg, "sandbox")
