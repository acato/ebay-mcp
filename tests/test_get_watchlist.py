"""Tests for the get_watchlist tool."""

from __future__ import annotations

import json
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from ebay_mcp.auth import _token_cache_path
from ebay_mcp.server import _normalize_trading_item


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


# --------------------- _normalize_trading_item ------------------------------


def test_normalize_full_item():
    raw = {
        "ItemID": "353528728623",
        "Title": "Cool Thing",
        "ViewItemURL": "https://www.ebay.com/itm/353528728623",
        "EndTime": "2026-06-05T10:00:00.000Z",
        "ListingType": "Auction",
        "Quantity": "1",
        "SellingStatus": {
            "CurrentPrice": {"_value": "12.50", "currencyID": "USD"},
            "BidCount": "3",
        },
        "Seller": {"UserID": "cool_seller"},
    }
    out = _normalize_trading_item(raw)
    assert out["item_id"] == "353528728623"
    assert out["title"] == "Cool Thing"
    assert out["price"] == 12.5
    assert out["currency"] == "USD"
    assert out["ends_at"] == "2026-06-05T10:00:00.000Z"
    assert out["bid_count"] == 3
    assert out["seller"] == "cool_seller"
    assert out["web_url"].startswith("https://")
    assert out["listing_type"] == "Auction"
    assert out["quantity_available"] == 1


def test_normalize_minimal_item():
    raw = {"ItemID": "1", "Title": "Bare item"}
    out = _normalize_trading_item(raw)
    assert out["item_id"] == "1"
    assert out["title"] == "Bare item"
    assert out["price"] is None
    assert out["currency"] is None
    assert out["bid_count"] is None
    assert out["seller"] is None
    assert out["web_url"] == ""


def test_normalize_handles_zero_price():
    """CurrentPrice "0.00" should normalize to None (no real price)."""
    raw = {
        "ItemID": "1",
        "Title": "Free?",
        "SellingStatus": {"CurrentPrice": {"_value": "0.00", "currencyID": "USD"}},
    }
    out = _normalize_trading_item(raw)
    assert out["price"] is None


# --------------------- get_watchlist end-to-end -----------------------------


@respx.mock
def test_get_watchlist_happy_path(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)

    respx.post("https://api.sandbox.ebay.com/ws/api.dll").mock(
        return_value=httpx.Response(
            200,
            content=b"""<?xml version="1.0"?>
            <GetMyeBayBuyingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
              <Ack>Success</Ack>
              <WatchList>
                <ItemArray>
                  <Item>
                    <ItemID>111</ItemID>
                    <Title>First</Title>
                    <ViewItemURL>https://www.ebay.com/itm/111</ViewItemURL>
                    <SellingStatus>
                      <CurrentPrice currencyID="USD">10.00</CurrentPrice>
                      <BidCount>2</BidCount>
                    </SellingStatus>
                    <Seller><UserID>seller1</UserID></Seller>
                    <ListingType>Auction</ListingType>
                  </Item>
                  <Item>
                    <ItemID>222</ItemID>
                    <Title>Second</Title>
                    <SellingStatus>
                      <CurrentPrice currencyID="USD">99.99</CurrentPrice>
                    </SellingStatus>
                    <ListingType>FixedPriceItem</ListingType>
                  </Item>
                </ItemArray>
                <PaginationResult>
                  <TotalNumberOfEntries>2</TotalNumberOfEntries>
                </PaginationResult>
              </WatchList>
            </GetMyeBayBuyingResponse>""",
        )
    )

    from ebay_mcp.server import get_watchlist

    result = get_watchlist()
    assert result["host"] == "sandbox"
    assert result["total"] == 2
    assert len(result["hits"]) == 2
    assert result["hits"][0]["item_id"] == "111"
    assert result["hits"][0]["price"] == 10.0
    assert result["hits"][0]["bid_count"] == 2
    assert result["hits"][1]["item_id"] == "222"
    assert result["hits"][1]["listing_type"] == "FixedPriceItem"


@respx.mock
def test_get_watchlist_single_item_returned_as_dict(monkeypatch, tmp_path):
    """Trading XML parser collapses single repeated tags to dict; we should still
    return a list."""
    _setup_env(monkeypatch, tmp_path)

    respx.post("https://api.sandbox.ebay.com/ws/api.dll").mock(
        return_value=httpx.Response(
            200,
            content=b"""<?xml version="1.0"?>
            <GetMyeBayBuyingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
              <Ack>Success</Ack>
              <WatchList>
                <ItemArray>
                  <Item><ItemID>999</ItemID><Title>Only one</Title></Item>
                </ItemArray>
                <PaginationResult><TotalNumberOfEntries>1</TotalNumberOfEntries></PaginationResult>
              </WatchList>
            </GetMyeBayBuyingResponse>""",
        )
    )

    from ebay_mcp.server import get_watchlist

    result = get_watchlist()
    assert result["total"] == 1
    assert len(result["hits"]) == 1
    assert result["hits"][0]["item_id"] == "999"


@respx.mock
def test_get_watchlist_empty(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)

    respx.post("https://api.sandbox.ebay.com/ws/api.dll").mock(
        return_value=httpx.Response(
            200,
            content=b"""<?xml version="1.0"?>
            <GetMyeBayBuyingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
              <Ack>Success</Ack>
              <WatchList>
                <ItemArray/>
                <PaginationResult><TotalNumberOfEntries>0</TotalNumberOfEntries></PaginationResult>
              </WatchList>
            </GetMyeBayBuyingResponse>""",
        )
    )

    from ebay_mcp.server import get_watchlist

    result = get_watchlist()
    assert result["total"] == 0
    assert result["hits"] == []


@respx.mock
def test_get_watchlist_pagination_converts_offset_to_page(monkeypatch, tmp_path):
    """offset=100 with limit=50 should become PageNumber=3."""
    _setup_env(monkeypatch, tmp_path)

    route = respx.post("https://api.sandbox.ebay.com/ws/api.dll").mock(
        return_value=httpx.Response(
            200,
            content=b"""<?xml version="1.0"?>
            <GetMyeBayBuyingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
              <Ack>Success</Ack>
              <WatchList>
                <ItemArray/>
                <PaginationResult><TotalNumberOfEntries>0</TotalNumberOfEntries></PaginationResult>
              </WatchList>
            </GetMyeBayBuyingResponse>""",
        )
    )

    from ebay_mcp.server import get_watchlist

    get_watchlist(limit=50, offset=100)
    body = route.calls.last.request.content
    assert b"<EntriesPerPage>50</EntriesPerPage>" in body
    assert b"<PageNumber>3</PageNumber>" in body


def test_get_watchlist_limit_validation(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    from ebay_mcp.server import get_watchlist

    with pytest.raises(ValueError, match="limit must be between 1 and 200"):
        get_watchlist(limit=0)
    with pytest.raises(ValueError, match="limit must be between 1 and 200"):
        get_watchlist(limit=999)
    with pytest.raises(ValueError, match="offset must be >= 0"):
        get_watchlist(offset=-1)
