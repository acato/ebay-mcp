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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from ebay_mcp.config import Config
from ebay_mcp.urls import urls_for_host

# Refresh tokens this many seconds before nominal expiry. Keeps each tool call
# from racing the clock against eBay's clock.
TOKEN_REFRESH_BUFFER_SECONDS = 300

# Default scopes for client_credentials flow. Only the public Browse scope is
# available at this grant level; user-scoped operations need authorization_code.
APP_TOKEN_SCOPES = "https://api.ebay.com/oauth/api_scope"


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
