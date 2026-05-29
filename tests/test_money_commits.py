"""Tests for place_bid / buy_now / make_best_offer.

Coverage groups:
  - Safety-gate refusals (structured payloads, not exceptions):
      * confirm_amount mismatch
      * amount > $500 cap with no override
      * max_bid_override set but lower than amount
  - Safety-gate bypasses:
      * max_bid_override >= amount
      * HIGH_VALUE_OVERRIDE_ENV=1
  - Happy paths for each of the three tools (mocked Trading API).
  - Production-host warning appears in successful responses.
  - eBay-side TradingApiError propagation.
  - Input-shape ValueErrors (empty item_id, non-positive amounts).
"""

from __future__ import annotations

import json
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from ebay_mcp.auth import _token_cache_path
from ebay_mcp.trading import TradingApiError

SANDBOX_URL = "https://api.sandbox.ebay.com/ws/api.dll"
PRODUCTION_URL = "https://api.ebay.com/ws/api.dll"


def _setup_env(monkeypatch, tmp_path: Path, host: str = "sandbox") -> None:
    """Write a config + cache a user token for the target host."""
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


def _place_offer_success_xml(*, action: str = "Bid", price: str = "25.00") -> bytes:
    """Canonical PlaceOffer happy-path response."""
    extras = "<HighBidder>true</HighBidder>" if action == "Bid" else ""
    return (
        f"""<?xml version="1.0"?>
        <PlaceOfferResponse xmlns="urn:ebay:apis:eBLBaseComponents">
          <Ack>Success</Ack>
          <CurrentPrice currencyID="USD">{price}</CurrentPrice>
          <MinimumToOutbid currencyID="USD">26.00</MinimumToOutbid>
          {extras}
        </PlaceOfferResponse>"""
    ).encode()


# --------------------- Refusal: confirm mismatch ----------------------------


def test_place_bid_confirm_mismatch_refused(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    from ebay_mcp.server import place_bid

    result = place_bid("353528728623", max_bid_amount=25.0, confirm_amount=24.0)
    assert result["refused"] is True
    assert result["reason"] == "confirm_mismatch"
    assert result["tool"] == "place_bid"
    assert result["amount"] == 25.0
    assert result["confirm_amount"] == 24.0
    assert "must equal" in result["message"]


def test_buy_now_confirm_mismatch_refused(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    # buy_now derives amount FROM confirm_amount, so we can't reach
    # this gate through the public API — call the shared helper with
    # a deliberate skew to exercise it.
    from ebay_mcp.server import _place_offer

    result = _place_offer(
        tool="buy_now",
        action="Purchase",
        item_id="x",
        amount=100.0,
        confirm_amount=99.0,
        quantity=1,
        currency="USD",
        max_bid_override=None,
        host=None,
    )
    assert result["refused"] is True
    assert result["reason"] == "confirm_mismatch"


def test_make_best_offer_confirm_mismatch_refused(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    from ebay_mcp.server import make_best_offer

    result = make_best_offer(
        "353528728623", offer_amount=15.0, confirm_amount=15.5
    )
    assert result["refused"] is True
    assert result["reason"] == "confirm_mismatch"
    assert result["tool"] == "make_best_offer"


# --------------------- Refusal: cap exceeded --------------------------------


def test_place_bid_cap_exceeded_refused(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    monkeypatch.delenv("EBAY_MCP_ALLOW_HIGH_VALUE", raising=False)
    from ebay_mcp.server import MONEY_CAP, place_bid

    result = place_bid("353528728623", max_bid_amount=600.0, confirm_amount=600.0)
    assert result["refused"] is True
    assert result["reason"] == "cap_exceeded"
    assert result["amount"] == 600.0
    assert result["cap"] == MONEY_CAP
    assert "EBAY_MCP_ALLOW_HIGH_VALUE" in result["message"]
    assert "max_bid_override" in result["message"]


def test_buy_now_cap_exceeded_refused(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    monkeypatch.delenv("EBAY_MCP_ALLOW_HIGH_VALUE", raising=False)
    from ebay_mcp.server import buy_now

    result = buy_now("353528728623", confirm_amount=750.0)
    assert result["refused"] is True
    assert result["reason"] == "cap_exceeded"
    assert result["tool"] == "buy_now"


def test_make_best_offer_cap_exceeded_refused(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    monkeypatch.delenv("EBAY_MCP_ALLOW_HIGH_VALUE", raising=False)
    from ebay_mcp.server import make_best_offer

    result = make_best_offer(
        "353528728623", offer_amount=999.99, confirm_amount=999.99
    )
    assert result["refused"] is True
    assert result["reason"] == "cap_exceeded"


# --------------------- Refusal: override too low ----------------------------


def test_place_bid_override_too_low_refused(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    monkeypatch.delenv("EBAY_MCP_ALLOW_HIGH_VALUE", raising=False)
    from ebay_mcp.server import place_bid

    # amount over cap, override present but insufficient
    result = place_bid(
        "353528728623",
        max_bid_amount=600.0,
        confirm_amount=600.0,
        max_bid_override=550.0,
    )
    # cap_exceeded fires first because override < amount fails the
    # per-call-override check (override_ok is False) AND env_override is
    # False, so we land in the cap-exceeded branch. The override_too_low
    # branch is reached when env is unset, max_bid_override < amount,
    # AND amount > cap — which is the same situation. Either reason is
    # correct semantically; assert one of the two.
    assert result["refused"] is True
    assert result["reason"] in ("cap_exceeded", "override_too_low")


# --------------------- Bypass: per-call override ----------------------------


@respx.mock
def test_place_bid_high_value_per_call_override_passes(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    monkeypatch.delenv("EBAY_MCP_ALLOW_HIGH_VALUE", raising=False)
    route = respx.post(SANDBOX_URL).mock(
        return_value=httpx.Response(200, content=_place_offer_success_xml(price="700.00"))
    )

    from ebay_mcp.server import place_bid

    result = place_bid(
        "353528728623",
        max_bid_amount=700.0,
        confirm_amount=700.0,
        max_bid_override=750.0,
    )
    assert result.get("refused") is None or result.get("refused") is False
    assert result["placed"] is True
    assert result["amount"] == 700.0
    assert route.called
    body = route.calls.last.request.content
    assert b"<Action>Bid</Action>" in body
    assert b'<MaxBid currencyID="USD">700.00</MaxBid>' in body


# --------------------- Bypass: env var --------------------------------------


@respx.mock
def test_buy_now_high_value_env_override_passes(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    monkeypatch.setenv("EBAY_MCP_ALLOW_HIGH_VALUE", "1")
    route = respx.post(SANDBOX_URL).mock(
        return_value=httpx.Response(
            200, content=_place_offer_success_xml(action="Purchase", price="2500.00")
        )
    )

    from ebay_mcp.server import buy_now

    result = buy_now("353528728623", confirm_amount=2500.0)
    assert result["placed"] is True
    assert result["action"] == "Purchase"
    assert result["amount"] == 2500.0
    body = route.calls.last.request.content
    assert b"<Action>Purchase</Action>" in body
    assert b'<MaxBid currencyID="USD">2500.00</MaxBid>' in body


# --------------------- Happy paths ------------------------------------------


@respx.mock
def test_place_bid_happy_path(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    route = respx.post(SANDBOX_URL).mock(
        return_value=httpx.Response(200, content=_place_offer_success_xml(price="25.00"))
    )

    from ebay_mcp.server import place_bid

    result = place_bid("353528728623", max_bid_amount=25.0, confirm_amount=25.0)
    assert result["host"] == "sandbox"
    assert result["tool"] == "place_bid"
    assert result["item_id"] == "353528728623"
    assert result["action"] == "Bid"
    assert result["amount"] == 25.0
    assert result["currency"] == "USD"
    assert result["quantity"] == 1
    assert result["placed"] is True
    assert result["current_price"] == {"value": 25.0, "currency": "USD"}
    assert result["minimum_to_outbid"] == {"value": 26.0, "currency": "USD"}
    assert result["high_bidder"] is True
    assert "warning" not in result  # sandbox

    req = route.calls.last.request
    assert req.headers.get("X-EBAY-API-CALL-NAME") == "PlaceOffer"
    assert b"<ItemID>353528728623</ItemID>" in req.content
    assert b"<EndUserIP>127.0.0.1</EndUserIP>" in req.content
    assert b"<Action>Bid</Action>" in req.content
    assert b'<MaxBid currencyID="USD">25.00</MaxBid>' in req.content


@respx.mock
def test_buy_now_happy_path(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    route = respx.post(SANDBOX_URL).mock(
        return_value=httpx.Response(
            200, content=_place_offer_success_xml(action="Purchase", price="49.99")
        )
    )

    from ebay_mcp.server import buy_now

    result = buy_now("353528728623", confirm_amount=49.99, quantity=2)
    assert result["placed"] is True
    assert result["action"] == "Purchase"
    assert result["amount"] == 49.99
    assert result["quantity"] == 2

    body = route.calls.last.request.content
    assert b"<Action>Purchase</Action>" in body
    assert b'<MaxBid currencyID="USD">49.99</MaxBid>' in body
    assert b"<Quantity>2</Quantity>" in body


@respx.mock
def test_make_best_offer_happy_path(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    route = respx.post(SANDBOX_URL).mock(
        return_value=httpx.Response(
            200,
            content=b"""<?xml version="1.0"?>
            <PlaceOfferResponse xmlns="urn:ebay:apis:eBLBaseComponents">
              <Ack>Success</Ack>
              <BestOfferID>987654</BestOfferID>
            </PlaceOfferResponse>""",
        )
    )

    from ebay_mcp.server import make_best_offer

    result = make_best_offer(
        "353528728623", offer_amount=15.0, confirm_amount=15.0
    )
    assert result["placed"] is True
    assert result["action"] == "BestOffer"
    assert result["amount"] == 15.0
    assert result["best_offer_id"] == "987654"

    body = route.calls.last.request.content
    assert b"<Action>BestOffer</Action>" in body
    # BestOffer uses OfferPrice, not MaxBid
    assert b'<OfferPrice currencyID="USD">15.00</OfferPrice>' in body
    assert b"<MaxBid" not in body


# --------------------- Production-host warning ------------------------------


@respx.mock
def test_production_host_emits_warning(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path, host="production")
    route = respx.post(PRODUCTION_URL).mock(
        return_value=httpx.Response(200, content=_place_offer_success_xml(price="25.00"))
    )

    from ebay_mcp.server import place_bid

    result = place_bid("353528728623", max_bid_amount=25.0, confirm_amount=25.0)
    assert result["host"] == "production"
    assert "warning" in result
    assert "PRODUCTION" in result["warning"]
    assert "real money" in result["warning"]
    assert route.called


# --------------------- TradingApiError propagation --------------------------


@respx.mock
def test_place_bid_propagates_trading_failure(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    respx.post(SANDBOX_URL).mock(
        return_value=httpx.Response(
            200,
            content=b"""<?xml version="1.0"?>
            <PlaceOfferResponse xmlns="urn:ebay:apis:eBLBaseComponents">
              <Ack>Failure</Ack>
              <Errors>
                <ErrorCode>478</ErrorCode>
                <ShortMessage>Bid amount is below minimum</ShortMessage>
              </Errors>
            </PlaceOfferResponse>""",
        )
    )

    from ebay_mcp.server import place_bid

    with pytest.raises(TradingApiError) as exc_info:
        place_bid("353528728623", max_bid_amount=1.0, confirm_amount=1.0)
    assert exc_info.value.call_name == "PlaceOffer"
    assert exc_info.value.errors["ErrorCode"] == "478"


# --------------------- Input-shape ValueErrors ------------------------------


def test_place_bid_empty_item_id_raises(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    from ebay_mcp.server import place_bid

    with pytest.raises(ValueError, match="item_id is required"):
        place_bid("", max_bid_amount=10.0, confirm_amount=10.0)
    with pytest.raises(ValueError, match="item_id is required"):
        place_bid("   ", max_bid_amount=10.0, confirm_amount=10.0)


def test_place_bid_non_positive_amount_raises(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    from ebay_mcp.server import place_bid

    with pytest.raises(ValueError, match="amount must be positive"):
        place_bid("353528728623", max_bid_amount=0, confirm_amount=0)
    with pytest.raises(ValueError, match="amount must be positive"):
        place_bid("353528728623", max_bid_amount=-5.0, confirm_amount=-5.0)


def test_buy_now_non_positive_confirm_raises(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    from ebay_mcp.server import buy_now

    # buy_now reuses confirm_amount as the amount; non-positive confirm
    # trips the amount validator first.
    with pytest.raises(ValueError, match="amount must be positive"):
        buy_now("353528728623", confirm_amount=0)


def test_make_best_offer_invalid_quantity_raises(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    from ebay_mcp.server import make_best_offer

    with pytest.raises(ValueError, match="quantity must be"):
        make_best_offer(
            "353528728623", offer_amount=10.0, confirm_amount=10.0, quantity=0
        )
