"""Tests for the get_item tool (Browse API)."""

from __future__ import annotations

import json
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from ebay_mcp.auth import _token_cache_path


def _setup_env(monkeypatch, tmp_path: Path) -> None:
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
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    cache = _token_cache_path("sandbox")
    cache.parent.mkdir(parents=True, exist_ok=True)
    expires_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    cache.write_text(
        json.dumps({"client_credentials": {"access_token": "TOKEN", "expires_at": expires_at}}),
        encoding="utf-8",
    )


@respx.mock
def test_get_item_happy(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    respx.get("https://api.sandbox.ebay.com/buy/browse/v1/item/v1|111|0").mock(
        return_value=httpx.Response(
            200,
            json={
                "itemId": "v1|111|0",
                "title": "Cool thing",
                "price": {"value": "42.00", "currency": "USD"},
                "condition": "New",
                "shortDescription": "A useful item",
                "shippingOptions": [{"shippingCost": {"value": "0.00"}}],
                "returnTerms": {"returnsAccepted": True},
                "itemLocation": {"city": "Seattle", "stateOrProvince": "WA"},
            },
        )
    )

    from ebay_mcp.server import get_item

    result = get_item("v1|111|0")

    assert result["host"] == "sandbox"
    assert result["item_id"] == "v1|111|0"
    assert result["price"] == 42.0
    assert result["description"] == "A useful item"
    assert result["shipping_options"] == [{"shippingCost": {"value": "0.00"}}]
    assert result["return_terms"] == {"returnsAccepted": True}
    assert result["item_location"]["city"] == "Seattle"


@respx.mock
def test_get_item_missing_returns_missing(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    respx.get("https://api.sandbox.ebay.com/buy/browse/v1/item/v1|9999|0").mock(
        return_value=httpx.Response(404, json={"errors": [{"message": "Not found"}]})
    )

    from ebay_mcp.server import get_item

    result = get_item("v1|9999|0")
    assert result == {"item_id": "v1|9999|0", "host": "sandbox", "missing": True}


@respx.mock
def test_get_item_500_propagates(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    respx.get("https://api.sandbox.ebay.com/buy/browse/v1/item/v1|500|0").mock(
        return_value=httpx.Response(500, json={"errors": [{"message": "server boom"}]})
    )

    from ebay_mcp.server import get_item

    with pytest.raises(httpx.HTTPStatusError):
        get_item("v1|500|0")


def test_get_item_empty_id_raises(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    from ebay_mcp.server import get_item

    with pytest.raises(ValueError, match="item_id is required"):
        get_item("")
