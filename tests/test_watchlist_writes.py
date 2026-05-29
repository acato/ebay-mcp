"""Tests for add_to_watchlist + remove_from_watchlist."""

from __future__ import annotations

import json
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from ebay_mcp.auth import _token_cache_path


def _setup_env(monkeypatch, tmp_path: Path, host: str = "sandbox") -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        textwrap.dedent(
            f"""
            default_host = "{host}"

            [hosts.{host}]
            app_id = "TheApp"
            dev_id = "TheDev"
            cert_id = "TheCert"
            redirect_uri = "Foo-Bar-{host}-xxxx"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("EBAY_MCP_CONFIG", str(cfg))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cache_file = _token_cache_path(host)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(
        json.dumps(
            {
                "user": {
                    "access_token": "USER_TOK",
                    "refresh_token": "R",
                    "access_expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                    "refresh_expires_at": (datetime.now(UTC) + timedelta(days=300)).isoformat(),
                }
            }
        ),
        encoding="utf-8",
    )


_TRADING_URL = "https://api.sandbox.ebay.com/ws/api.dll"


# --------------------- add_to_watchlist -------------------------------------


@respx.mock
def test_add_to_watchlist_happy(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    route = respx.post(_TRADING_URL).mock(
        return_value=httpx.Response(
            200,
            content=b"""<?xml version="1.0"?>
            <AddToWatchListResponse xmlns="urn:ebay:apis:eBLBaseComponents">
              <Ack>Success</Ack>
              <WatchListCount>7</WatchListCount>
            </AddToWatchListResponse>""",
        )
    )

    from ebay_mcp.server import add_to_watchlist

    result = add_to_watchlist("353528728623")
    assert result == {
        "host": "sandbox",
        "item_id": "353528728623",
        "added": True,
        "watch_list_count": 7,
    }

    # Verify the request body and headers
    req = route.calls.last.request
    assert req.headers.get("X-EBAY-API-CALL-NAME") == "AddToWatchList"
    assert b"<ItemID>353528728623</ItemID>" in req.content


def test_add_to_watchlist_empty_id_raises(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    from ebay_mcp.server import add_to_watchlist

    with pytest.raises(ValueError, match="item_id is required"):
        add_to_watchlist("")
    with pytest.raises(ValueError, match="item_id is required"):
        add_to_watchlist("   ")


@respx.mock
def test_add_to_watchlist_propagates_failure(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    respx.post(_TRADING_URL).mock(
        return_value=httpx.Response(
            200,
            content=b"""<?xml version="1.0"?>
            <AddToWatchListResponse xmlns="urn:ebay:apis:eBLBaseComponents">
              <Ack>Failure</Ack>
              <Errors>
                <ErrorCode>21916403</ErrorCode>
                <ShortMessage>Item is already on the watchlist</ShortMessage>
              </Errors>
            </AddToWatchListResponse>""",
        )
    )

    from ebay_mcp.server import add_to_watchlist
    from ebay_mcp.trading import TradingApiError

    with pytest.raises(TradingApiError):
        add_to_watchlist("353528728623")


# --------------------- remove_from_watchlist --------------------------------


@respx.mock
def test_remove_from_watchlist_happy(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    route = respx.post(_TRADING_URL).mock(
        return_value=httpx.Response(
            200,
            content=b"""<?xml version="1.0"?>
            <RemoveFromWatchListResponse xmlns="urn:ebay:apis:eBLBaseComponents">
              <Ack>Success</Ack>
              <WatchListCount>5</WatchListCount>
            </RemoveFromWatchListResponse>""",
        )
    )

    from ebay_mcp.server import remove_from_watchlist

    result = remove_from_watchlist("353528728623")
    assert result == {
        "host": "sandbox",
        "item_id": "353528728623",
        "removed": True,
        "watch_list_count": 5,
    }

    req = route.calls.last.request
    assert req.headers.get("X-EBAY-API-CALL-NAME") == "RemoveFromWatchList"
    assert b"<ItemID>353528728623</ItemID>" in req.content


def test_remove_from_watchlist_empty_id_raises(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    from ebay_mcp.server import remove_from_watchlist

    with pytest.raises(ValueError, match="item_id is required"):
        remove_from_watchlist("")


@respx.mock
def test_remove_from_watchlist_count_missing(monkeypatch, tmp_path):
    """eBay sometimes omits WatchListCount; we should return -1 rather than crash."""
    _setup_env(monkeypatch, tmp_path)
    respx.post(_TRADING_URL).mock(
        return_value=httpx.Response(
            200,
            content=b"""<?xml version="1.0"?>
            <RemoveFromWatchListResponse xmlns="urn:ebay:apis:eBLBaseComponents">
              <Ack>Success</Ack>
            </RemoveFromWatchListResponse>""",
        )
    )

    from ebay_mcp.server import remove_from_watchlist

    result = remove_from_watchlist("999")
    assert result["watch_list_count"] == -1
    assert result["removed"] is True
