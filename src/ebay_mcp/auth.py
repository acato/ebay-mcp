"""OAuth2 authentication for eBay APIs.

App-level (client_credentials) tokens are cached per-host at
~/.config/ebay-mcp/token-cache-<host>.json under the "client_credentials" key.
User-level tokens (authorization_code flow) land in the same file under "user"
on Day 2.

Tokens auto-refresh when within `TOKEN_REFRESH_BUFFER_SECONDS` of expiry so a
mid-call refresh never races against the API request.
"""

from __future__ import annotations

import base64
import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from ebay_mcp.config import Config
from ebay_mcp.urls import urls_for_host

# Refresh tokens this many seconds before nominal expiry. Keeps each tool call
# from racing the clock against eBay's clock.
TOKEN_REFRESH_BUFFER_SECONDS = 300

# Default scopes for client_credentials flow. Only the public Browse scope is
# available at this grant level; user-scoped operations need authorization_code.
APP_TOKEN_SCOPES = "https://api.ebay.com/oauth/api_scope"

# User-scoped OAuth2 scopes needed for the v0.2+ tool surface (MyeBay reads,
# watchlist read/write, bid/buy via Trading API IAF tokens).
USER_TOKEN_SCOPES: tuple[str, ...] = (
    "https://api.ebay.com/oauth/api_scope",
    "https://api.ebay.com/oauth/api_scope/buy.order.readonly",
    "https://api.ebay.com/oauth/api_scope/buy.marketing",
    "https://api.ebay.com/oauth/api_scope/buy.guest.order",
)


class UserNotAuthenticated(Exception):
    """No user token cached, or the cached refresh_token has expired.

    Recovery path: call start_user_auth → open the URL → complete_user_auth
    with the returned code.
    """


def _token_cache_path(host: str) -> Path:
    return Path.home() / ".config" / "ebay-mcp" / f"token-cache-{host}.json"


def _load_cache(host: str) -> dict[str, Any]:
    p = _token_cache_path(host)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Corrupt or unreadable cache → treat as empty; we'll overwrite on next save.
        return {}


def _save_cache(host: str, data: dict[str, Any]) -> None:
    p = _token_cache_path(host)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _parse_iso(s: str) -> datetime:
    """Parse an ISO 8601 timestamp; accepts both 'Z' and '+00:00' suffixes."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def get_app_token(config: Config, host: str, *, client: httpx.Client | None = None) -> str:
    """Return a Bearer access token for app-level Browse API calls.

    Reads from cache if a fresh token is available; otherwise POSTs to eBay's
    OAuth2 token endpoint and caches the result. Idempotent — calling it many
    times in close succession yields the same cached token.

    Args:
        config: loaded Config.
        host: which configured host to authenticate against ("sandbox" or
            "production"). Tokens are per-host; sandbox tokens won't work
            against production endpoints and vice versa.
        client: optional httpx.Client for tests to inject a transport.
            Defaults to a fresh httpx.Client.

    Raises:
        ValueError: no cert_id available from env or config file.
        httpx.HTTPStatusError: eBay rejected the credentials.
    """
    cache = _load_cache(host)
    cc = cache.get("client_credentials")
    if cc and cc.get("expires_at"):
        try:
            expires_at = _parse_iso(cc["expires_at"])
            if expires_at - _now_utc() > timedelta(seconds=TOKEN_REFRESH_BUFFER_SECONDS):
                return cc["access_token"]
        except (ValueError, KeyError):
            # Malformed cache entry — fall through to refresh.
            pass

    return _fetch_app_token(config, host, cache, client=client)


def _fetch_app_token(
    config: Config,
    host: str,
    cache: dict[str, Any],
    *,
    client: httpx.Client | None,
) -> str:
    """POST client_credentials to eBay's token endpoint; persist + return the token."""
    host_cfg = config.hosts[host]
    cert_id = config.resolve_cert_id(host)
    urls = urls_for_host(host)

    basic_auth = base64.b64encode(f"{host_cfg.app_id}:{cert_id}".encode()).decode()
    request_kwargs = {
        "headers": {
            "Authorization": f"Basic {basic_auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        "data": {
            "grant_type": "client_credentials",
            "scope": APP_TOKEN_SCOPES,
        },
        "timeout": 30,
    }

    if client is not None:
        response = client.post(urls["oauth_token"], **request_kwargs)
    else:
        with httpx.Client() as c:
            response = c.post(urls["oauth_token"], **request_kwargs)
    response.raise_for_status()
    body = response.json()

    expires_in = int(body.get("expires_in", 7200))
    expires_at = _now_utc() + timedelta(seconds=expires_in)
    cache["client_credentials"] = {
        "access_token": body["access_token"],
        "token_type": body.get("token_type", "Bearer"),
        "expires_at": expires_at.isoformat(),
        "scope": body.get("scope", APP_TOKEN_SCOPES),
    }
    _save_cache(host, cache)
    return body["access_token"]


def clear_token_cache(host: str) -> None:
    """Delete the token cache file for a host. Diagnostic / recovery."""
    p = _token_cache_path(host)
    if p.exists():
        p.unlink()


# --------------------- User authorization_code flow --------------------------


def start_user_auth(config: Config, host: str) -> dict[str, Any]:
    """Construct an OAuth2 authorization URL for the manual code-paste flow.

    The user opens `auth_url` in a browser, signs in to eBay, and grants the
    requested scopes. eBay redirects to the host's configured `redirect_uri`
    with `?code=<authorization_code>&state=<state>`. The user copies the
    code and passes it to `complete_user_auth`.

    The `state` value is generated and persisted to the token cache under
    `pending_user_auth`, so `complete_user_auth` can verify it matches what
    eBay returns (basic CSRF / replay defense).
    """
    host_cfg = config.hosts[host]
    urls = urls_for_host(host)
    state = secrets.token_urlsafe(24)
    params = {
        "client_id": host_cfg.app_id,
        "response_type": "code",
        "redirect_uri": host_cfg.redirect_uri,
        "scope": " ".join(USER_TOKEN_SCOPES),
        "state": state,
    }
    auth_url = f"{urls['oauth_authorize']}?{urlencode(params)}"

    cache = _load_cache(host)
    cache["pending_user_auth"] = {
        "state": state,
        "started_at": _now_utc().isoformat(),
    }
    _save_cache(host, cache)

    return {
        "host": host,
        "auth_url": auth_url,
        "state": state,
        "instructions": (
            "1. Open `auth_url` in a browser.\n"
            "2. Sign in to eBay and grant the requested permissions.\n"
            "3. eBay redirects to your configured redirect_uri with "
            "?code=<code>&state=<state>. The redirect may show a broken page "
            "if redirect_uri isn't a real server — that's expected. Copy the "
            "`code` parameter from the URL bar.\n"
            "4. Call complete_user_auth(code, host) with that code."
        ),
    }


def complete_user_auth(
    config: Config,
    host: str,
    code: str,
    *,
    client: httpx.Client | None = None,
    state: str | None = None,
) -> dict[str, Any]:
    """Exchange an authorization code for user access + refresh tokens.

    If `state` is provided, verify it matches the value stored by start_user_auth.
    Mismatches are surfaced as a refusal (not raised) so the LLM can re-issue.
    """
    cache = _load_cache(host)
    pending = cache.get("pending_user_auth") or {}

    if state is not None and pending.get("state") and state != pending["state"]:
        return {
            "refused": True,
            "reason": "state_mismatch",
            "host": host,
            "message": "state value does not match the one issued by start_user_auth",
        }

    host_cfg = config.hosts[host]
    cert_id = config.resolve_cert_id(host)
    urls = urls_for_host(host)

    basic_auth = base64.b64encode(f"{host_cfg.app_id}:{cert_id}".encode()).decode()
    request_kwargs = {
        "headers": {
            "Authorization": f"Basic {basic_auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        "data": {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": host_cfg.redirect_uri,
        },
        "timeout": 30,
    }
    if client is not None:
        response = client.post(urls["oauth_token"], **request_kwargs)
    else:
        with httpx.Client() as c:
            response = c.post(urls["oauth_token"], **request_kwargs)
    response.raise_for_status()
    body = response.json()

    access_expires_in = int(body.get("expires_in", 7200))
    # eBay's refresh_token default is 18 months (47304000s). Fall back to that.
    refresh_expires_in = int(body.get("refresh_token_expires_in", 47_304_000))
    now = _now_utc()

    cache["user"] = {
        "access_token": body["access_token"],
        "refresh_token": body["refresh_token"],
        "token_type": body.get("token_type", "Bearer"),
        "access_expires_at": (now + timedelta(seconds=access_expires_in)).isoformat(),
        "refresh_expires_at": (now + timedelta(seconds=refresh_expires_in)).isoformat(),
        "scope": body.get("scope", " ".join(USER_TOKEN_SCOPES)),
    }
    cache.pop("pending_user_auth", None)
    _save_cache(host, cache)

    return {
        "host": host,
        "authenticated": True,
        "access_expires_at": cache["user"]["access_expires_at"],
        "refresh_expires_at": cache["user"]["refresh_expires_at"],
    }


def get_user_token(config: Config, host: str, *, client: httpx.Client | None = None) -> str:
    """Return a Bearer user access token, auto-refreshing if near expiry.

    Raises:
        UserNotAuthenticated: no user token cached, or refresh_token expired.
        httpx.HTTPStatusError: refresh request rejected by eBay.
    """
    cache = _load_cache(host)
    user_data = cache.get("user")
    if not user_data:
        raise UserNotAuthenticated(
            f"no user token cached for host {host!r}. "
            f"Call start_user_auth + complete_user_auth first."
        )

    expires_at_str = user_data.get("access_expires_at")
    if expires_at_str:
        try:
            expires_at = _parse_iso(expires_at_str)
            if expires_at - _now_utc() > timedelta(seconds=TOKEN_REFRESH_BUFFER_SECONDS):
                return user_data["access_token"]
        except (ValueError, KeyError):
            # Malformed timestamp → fall through to refresh.
            pass

    return _refresh_user_token(config, host, cache, client=client)


def _refresh_user_token(
    config: Config,
    host: str,
    cache: dict[str, Any],
    *,
    client: httpx.Client | None,
) -> str:
    """Use refresh_token to get a new access_token; persist + return it."""
    user_data = cache.get("user") or {}
    refresh_token = user_data.get("refresh_token")
    if not refresh_token:
        raise UserNotAuthenticated(f"no refresh_token for host {host!r}; cannot refresh")

    refresh_expires_str = user_data.get("refresh_expires_at")
    if refresh_expires_str:
        try:
            refresh_expires = _parse_iso(refresh_expires_str)
            if refresh_expires < _now_utc():
                raise UserNotAuthenticated(
                    f"refresh_token for host {host!r} expired at "
                    f"{refresh_expires_str}; re-authenticate via "
                    f"start_user_auth + complete_user_auth"
                )
        except UserNotAuthenticated:
            raise
        except (ValueError, KeyError):
            pass  # malformed → let eBay reject the refresh

    host_cfg = config.hosts[host]
    cert_id = config.resolve_cert_id(host)
    urls = urls_for_host(host)

    basic_auth = base64.b64encode(f"{host_cfg.app_id}:{cert_id}".encode()).decode()
    request_kwargs = {
        "headers": {
            "Authorization": f"Basic {basic_auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        "data": {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": user_data.get("scope") or " ".join(USER_TOKEN_SCOPES),
        },
        "timeout": 30,
    }
    if client is not None:
        response = client.post(urls["oauth_token"], **request_kwargs)
    else:
        with httpx.Client() as c:
            response = c.post(urls["oauth_token"], **request_kwargs)
    response.raise_for_status()
    body = response.json()

    access_expires_in = int(body.get("expires_in", 7200))
    now = _now_utc()
    user_data["access_token"] = body["access_token"]
    user_data["access_expires_at"] = (now + timedelta(seconds=access_expires_in)).isoformat()
    # eBay typically does not rotate the refresh_token; keep the existing one
    # unless a new one is explicitly returned.
    if body.get("refresh_token"):
        user_data["refresh_token"] = body["refresh_token"]
    cache["user"] = user_data
    _save_cache(host, cache)
    return body["access_token"]
