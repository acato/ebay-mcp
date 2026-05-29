"""Tests for the search tool (Browse API)."""

from __future__ import annotations

import json
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from ebay_mcp.auth import _token_cache_path
from ebay_mcp.server import _build_filter, _normalize_item_summary


def _setup_env(monkeypatch, tmp_path: Path) -> None:
    """Wire up config + a pre-populated fresh token so search() doesn't try to auth."""
    # Config
    cfg = tmp_path / "config.toml"
    cfg.write_text(
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
    monkeypatch.setenv("EBAY_MCP_CONFIG", str(cfg))

    # Token cache, with home redirected
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cache = _token_cache_path("sandbox")
    cache.parent.mkdir(parents=True, exist_ok=True)
    expires_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    cache.write_text(
        json.dumps(
            {"client_credentials": {"access_token": "TEST_TOKEN", "expires_at": expires_at}}
        ),
        encoding="utf-8",
    )


# --------------------- _build_filter ----------------------------------------


def test_filter_empty_returns_none():
    assert _build_filter(condition=None, min_price=None, max_price=None, currency="USD") is None


def test_filter_condition_only():
    f = _build_filter(condition="NEW", min_price=None, max_price=None, currency="USD")
    assert f == "conditions:{NEW}"


def test_filter_price_range():
    f = _build_filter(condition=None, min_price=10.0, max_price=100.0, currency="USD")
    assert "price:[10..100]" in f
    assert "priceCurrency:USD" in f


def test_filter_min_only():
    f = _build_filter(condition=None, min_price=10.0, max_price=None, currency="USD")
    assert "price:[10..]" in f


def test_filter_max_only():
    f = _build_filter(condition=None, min_price=None, max_price=50.0, currency="EUR")
    assert "price:[..50]" in f
    assert "priceCurrency:EUR" in f


def test_filter_all_combined():
    f = _build_filter(condition="USED_GOOD", min_price=5.0, max_price=99.99, currency="USD")
    parts = f.split(",")
    assert "conditions:{USED_GOOD}" in parts
    assert "price:[5..99.99]" in parts
    assert "priceCurrency:USD" in parts


# --------------------- _normalize_item_summary ------------------------------


def test_normalize_minimal():
    raw = {"itemId": "v1|123|0", "title": "Test", "price": {"value": "9.99", "currency": "USD"}}
    out = _normalize_item_summary(raw)
    assert out["item_id"] == "v1|123|0"
    assert out["title"] == "Test"
    assert out["price"] == 9.99
    assert out["currency"] == "USD"


def test_normalize_with_auction_fields():
    raw = {
        "itemId": "v1|456|0",
        "title": "Auction",
        "price": {"value": "5.00", "currency": "USD"},
        "bidCount": 3,
        "itemEndDate": "2026-06-05T10:00:00Z",
        "buyingOptions": ["AUCTION"],
    }
    out = _normalize_item_summary(raw)
    assert out["bid_count"] == 3
    assert out["ends_at"] == "2026-06-05T10:00:00Z"
    assert out["buying_options"] == ["AUCTION"]


def test_normalize_missing_price():
    raw = {"itemId": "v1|789|0", "title": "No price"}
    out = _normalize_item_summary(raw)
    assert out["price"] is None
    assert out["currency"] is None


# --------------------- search() end-to-end via respx ------------------------


_SANDBOX_SEARCH = "https://api.sandbox.ebay.com/buy/browse/v1/item_summary/search"


@respx.mock
def test_search_happy_path(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    route = respx.get(_SANDBOX_SEARCH).mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 42,
                "itemSummaries": [
                    {
                        "itemId": "v1|111|0",
                        "title": "Widget",
                        "price": {"value": "12.50", "currency": "USD"},
                        "condition": "New",
                    },
                    {
                        "itemId": "v1|222|0",
                        "title": "Gadget",
                        "price": {"value": "30.00", "currency": "USD"},
                        "condition": "Used",
                    },
                ],
            },
        )
    )

    from ebay_mcp.server import search

    result = search("widget")

    assert result["host"] == "sandbox"
    assert result["total"] == 42
    assert result["limit"] == 20
    assert result["offset"] == 0
    assert result["next_offset"] == 20
    assert len(result["hits"]) == 2
    assert result["hits"][0]["item_id"] == "v1|111|0"

    # Authorization header was sent.
    req = route.calls.last.request
    assert req.headers.get("Authorization") == "Bearer TEST_TOKEN"
    assert req.headers.get("X-EBAY-C-MARKETPLACE-ID") == "EBAY_US"


@respx.mock
def test_search_passes_filters_and_sort(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    route = respx.get(_SANDBOX_SEARCH).mock(
        return_value=httpx.Response(200, json={"total": 0, "itemSummaries": []})
    )

    from ebay_mcp.server import search

    search(
        "watch",
        condition="USED_GOOD",
        min_price=10,
        max_price=100,
        sort="price_asc",
    )

    req = route.calls.last.request
    qs = dict(req.url.params)
    assert qs["q"] == "watch"
    assert qs["sort"] == "price"  # price_asc → "price"
    assert "conditions:{USED_GOOD}" in qs["filter"]
    assert "price:[10..100]" in qs["filter"]


@respx.mock
def test_search_no_results(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    respx.get(_SANDBOX_SEARCH).mock(
        return_value=httpx.Response(200, json={"total": 0, "itemSummaries": []})
    )
    from ebay_mcp.server import search

    result = search("nonexistent")
    assert result["total"] == 0
    assert result["hits"] == []
    assert result["next_offset"] is None


@respx.mock
def test_search_last_page_has_no_next_offset(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    respx.get(_SANDBOX_SEARCH).mock(
        return_value=httpx.Response(200, json={"total": 5, "itemSummaries": []})
    )
    from ebay_mcp.server import search

    result = search("x", limit=10, offset=0)
    assert result["total"] == 5
    assert result["next_offset"] is None


# --------------------- search() validation ----------------------------------


def test_search_empty_query_raises(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    from ebay_mcp.server import search

    with pytest.raises(ValueError, match="query is required"):
        search("")


def test_search_invalid_sort_raises(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    from ebay_mcp.server import search

    with pytest.raises(ValueError, match="sort must be one of"):
        search("x", sort="random")


def test_search_invalid_condition_raises(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    from ebay_mcp.server import search

    with pytest.raises(ValueError, match="condition must be one of"):
        search("x", condition="kinda_used")


def test_search_limit_too_high_raises(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    from ebay_mcp.server import SEARCH_LIMIT_MAX, search

    with pytest.raises(ValueError, match="limit must be between"):
        search("x", limit=SEARCH_LIMIT_MAX + 1)


def test_search_negative_offset_raises(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    from ebay_mcp.server import search

    with pytest.raises(ValueError, match="offset must be >= 0"):
        search("x", offset=-1)


# --------------------- host parameter ---------------------------------------


@respx.mock
def test_search_explicit_host_overrides_default(monkeypatch, tmp_path):
    # Config has both hosts; default is sandbox; we pass host="production".
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        textwrap.dedent(
            """
            default_host = "sandbox"

            [hosts.sandbox]
            app_id = "S"
            dev_id = "D"
            cert_id = "C"

            [hosts.production]
            app_id = "PS"
            dev_id = "PD"
            cert_id = "PC"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("EBAY_MCP_CONFIG", str(cfg))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    # Pre-cached production token
    prod_cache = _token_cache_path("production")
    prod_cache.parent.mkdir(parents=True, exist_ok=True)
    prod_cache.write_text(
        json.dumps(
            {
                "client_credentials": {
                    "access_token": "PROD_TOKEN",
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                }
            }
        ),
        encoding="utf-8",
    )

    sandbox_route = respx.get(_SANDBOX_SEARCH)
    prod_route = respx.get("https://api.ebay.com/buy/browse/v1/item_summary/search").mock(
        return_value=httpx.Response(200, json={"total": 0, "itemSummaries": []})
    )

    from ebay_mcp.server import search

    result = search("x", host="production")
    assert result["host"] == "production"
    assert prod_route.called
    assert not sandbox_route.called
    assert prod_route.calls.last.request.headers.get("Authorization") == "Bearer PROD_TOKEN"
