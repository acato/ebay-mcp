"""Tests for the Trading API XML client."""

from __future__ import annotations

import json
import textwrap
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from ebay_mcp.auth import _token_cache_path
from ebay_mcp.config import load_config
from ebay_mcp.trading import (
    COMPATIBILITY_LEVEL,
    SITE_ID_US,
    TRADING_NS,
    TradingApiError,
    build_request_xml,
    parse_response_xml,
    trading_call,
)


def _config_with_user_token(tmp_path: Path, host: str = "sandbox") -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        textwrap.dedent(
            f"""
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
    return cfg


def _seed_user_token(host: str, access_token: str = "USER_TOK") -> None:
    cache_file = _token_cache_path(host)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(
        json.dumps(
            {
                "user": {
                    "access_token": access_token,
                    "refresh_token": "R",
                    "access_expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                    "refresh_expires_at": (datetime.now(UTC) + timedelta(days=300)).isoformat(),
                }
            }
        ),
        encoding="utf-8",
    )


# --------------------- build_request_xml ------------------------------------


def _localname(tag: str) -> str:
    """Return the local-name (no namespace) of a tag — ElementTree prefixes
    parsed elements with `{namespace}` when the document declared one."""
    return tag.split("}", 1)[1] if "}" in tag else tag


def test_build_request_xml_simple():
    out = build_request_xml("GetMyeBayBuying", {"WatchList": {"Include": True}})
    assert TRADING_NS in out  # default namespace baked into the root element
    root = ET.fromstring(out)
    assert _localname(root.tag) == "GetMyeBayBuyingRequest"

    watch = next(c for c in root if _localname(c.tag) == "WatchList")
    include = next(c for c in watch if _localname(c.tag) == "Include")
    assert include.text == "true"  # bools lowercase


def test_build_request_xml_nested():
    out = build_request_xml(
        "AddItem",
        {
            "Item": {
                "Title": "Cool gadget",
                "Quantity": 1,
                "PrimaryCategory": {"CategoryID": "9355"},
            }
        },
    )
    root = ET.fromstring(out)
    ns = f"{{{TRADING_NS}}}"
    title = root.find(f".//{ns}Title")
    assert title is not None and title.text == "Cool gadget"
    category = root.find(f".//{ns}PrimaryCategory/{ns}CategoryID")
    assert category is not None and category.text == "9355"


def test_build_request_xml_bool_lowercase():
    out = build_request_xml("X", {"flag": False})
    root = ET.fromstring(out)
    flag = next(c for c in root if _localname(c.tag) == "flag")
    assert flag.text == "false"


def test_build_request_xml_empty_payload():
    out = build_request_xml("GeteBayOfficialTime", {})
    root = ET.fromstring(out)
    assert _localname(root.tag) == "GeteBayOfficialTimeRequest"
    assert list(root) == []


def test_build_request_xml_rejects_raw_list():
    with pytest.raises(ValueError, match="lists must be wrapped"):
        build_request_xml("X", {"Items": [1, 2, 3]})


# --------------------- parse_response_xml -----------------------------------


def test_parse_response_xml_strips_namespace():
    xml_bytes = b"""<?xml version="1.0"?>
    <GetMyeBayBuyingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <Ack>Success</Ack>
      <Version>1267</Version>
    </GetMyeBayBuyingResponse>"""
    parsed = parse_response_xml(xml_bytes)
    assert parsed["Ack"] == "Success"
    assert parsed["Version"] == "1267"


def test_parse_response_xml_collapses_repeats_to_list():
    xml_bytes = b"""<?xml version="1.0"?>
    <Response xmlns="urn:ebay:apis:eBLBaseComponents">
      <ItemArray>
        <Item><ItemID>1</ItemID></Item>
        <Item><ItemID>2</ItemID></Item>
        <Item><ItemID>3</ItemID></Item>
      </ItemArray>
    </Response>"""
    parsed = parse_response_xml(xml_bytes)
    items = parsed["ItemArray"]["Item"]
    assert isinstance(items, list)
    assert [it["ItemID"] for it in items] == ["1", "2", "3"]


def test_parse_response_xml_keeps_attributes_with_value():
    xml_bytes = b"""<?xml version="1.0"?>
    <Response xmlns="urn:ebay:apis:eBLBaseComponents">
      <CurrentPrice currencyID="USD">12.99</CurrentPrice>
    </Response>"""
    parsed = parse_response_xml(xml_bytes)
    cp = parsed["CurrentPrice"]
    assert cp["_value"] == "12.99"
    assert cp["currencyID"] == "USD"


# --------------------- trading_call -----------------------------------------


@respx.mock
def test_trading_call_sends_required_headers(monkeypatch, tmp_path):
    cfg = load_config(_config_with_user_token(tmp_path, "sandbox"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _seed_user_token("sandbox", "FAKE_USER_TOKEN")

    route = respx.post("https://api.sandbox.ebay.com/ws/api.dll").mock(
        return_value=httpx.Response(
            200,
            content=b"""<?xml version="1.0"?>
            <GetMyeBayBuyingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
              <Ack>Success</Ack>
            </GetMyeBayBuyingResponse>""",
        )
    )

    trading_call(cfg, "sandbox", "GetMyeBayBuying", {"WatchList": {"Include": True}})

    req = route.calls.last.request
    assert req.headers.get("X-EBAY-API-CALL-NAME") == "GetMyeBayBuying"
    assert req.headers.get("X-EBAY-API-COMPATIBILITY-LEVEL") == COMPATIBILITY_LEVEL
    assert req.headers.get("X-EBAY-API-SITEID") == SITE_ID_US
    assert req.headers.get("X-EBAY-API-DEV-NAME") == "TheDev"
    assert req.headers.get("X-EBAY-API-APP-NAME") == "TheApp"
    assert req.headers.get("X-EBAY-API-CERT-NAME") == "TheCert"
    assert req.headers.get("X-EBAY-API-IAF-TOKEN") == "FAKE_USER_TOKEN"
    assert req.headers.get("Content-Type", "").startswith("text/xml")
    # Body is the request XML
    assert b"<WatchList>" in req.content
    assert b"<Include>true</Include>" in req.content


@respx.mock
def test_trading_call_returns_parsed_response(monkeypatch, tmp_path):
    cfg = load_config(_config_with_user_token(tmp_path, "sandbox"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _seed_user_token("sandbox")

    respx.post("https://api.sandbox.ebay.com/ws/api.dll").mock(
        return_value=httpx.Response(
            200,
            content=b"""<?xml version="1.0"?>
            <GetMyeBayBuyingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
              <Ack>Success</Ack>
              <WatchList>
                <ItemArray>
                  <Item><ItemID>111</ItemID><Title>X</Title></Item>
                </ItemArray>
                <PaginationResult><TotalNumberOfEntries>1</TotalNumberOfEntries></PaginationResult>
              </WatchList>
            </GetMyeBayBuyingResponse>""",
        )
    )

    parsed = trading_call(cfg, "sandbox", "GetMyeBayBuying", {})
    assert parsed["Ack"] == "Success"
    assert parsed["WatchList"]["ItemArray"]["Item"]["ItemID"] == "111"
    assert parsed["WatchList"]["PaginationResult"]["TotalNumberOfEntries"] == "1"


@respx.mock
def test_trading_call_raises_on_failure_ack(monkeypatch, tmp_path):
    cfg = load_config(_config_with_user_token(tmp_path, "sandbox"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _seed_user_token("sandbox")

    respx.post("https://api.sandbox.ebay.com/ws/api.dll").mock(
        return_value=httpx.Response(
            200,
            content=b"""<?xml version="1.0"?>
            <GetMyeBayBuyingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
              <Ack>Failure</Ack>
              <Errors>
                <ErrorCode>17</ErrorCode>
                <ShortMessage>Bad call</ShortMessage>
              </Errors>
            </GetMyeBayBuyingResponse>""",
        )
    )

    with pytest.raises(TradingApiError) as exc_info:
        trading_call(cfg, "sandbox", "GetMyeBayBuying", {})
    assert exc_info.value.call_name == "GetMyeBayBuying"
    assert exc_info.value.errors["ErrorCode"] == "17"


@respx.mock
def test_trading_call_propagates_500(monkeypatch, tmp_path):
    cfg = load_config(_config_with_user_token(tmp_path, "sandbox"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _seed_user_token("sandbox")

    respx.post("https://api.sandbox.ebay.com/ws/api.dll").mock(
        return_value=httpx.Response(500, text="oops")
    )

    with pytest.raises(httpx.HTTPStatusError):
        trading_call(cfg, "sandbox", "GetMyeBayBuying", {})


@respx.mock
def test_trading_call_routes_to_production_endpoint(monkeypatch, tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        textwrap.dedent(
            """
            [hosts.production]
            app_id = "P-App"
            dev_id = "P-Dev"
            cert_id = "P-Cert"
            redirect_uri = "Foo-Bar-prd-xxxx"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    cfg = load_config(p)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _seed_user_token("production", "PRD_USER_TOK")

    sandbox_route = respx.post("https://api.sandbox.ebay.com/ws/api.dll")
    prod_route = respx.post("https://api.ebay.com/ws/api.dll").mock(
        return_value=httpx.Response(
            200,
            content=b"""<?xml version="1.0"?>
            <X xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Success</Ack></X>""",
        )
    )

    trading_call(cfg, "production", "GetMyeBayBuying", {})
    assert prod_route.called
    assert not sandbox_route.called
    assert prod_route.calls.last.request.headers.get("X-EBAY-API-IAF-TOKEN") == "PRD_USER_TOK"
