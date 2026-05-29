"""Live integration tests against api.sandbox.ebay.com.

OPT-IN required: set EBAY_MCP_LIVE=1.

These tests use the configured sandbox host's keys to:
  1. Fetch an app-level OAuth token via client_credentials.
  2. Issue a real search against the sandbox Browse API.
  3. Fetch one item by ID.

No money is at risk — sandbox uses fake currency. Tests skip if the sandbox
host has no credentials configured or if the search returns zero results
(common in sandbox; we can't seed listings).
"""

from __future__ import annotations

import os

import pytest

LIVE_ENABLED = os.environ.get("EBAY_MCP_LIVE") == "1"

pytestmark = pytest.mark.skipif(
    not LIVE_ENABLED,
    reason="live tests skipped — set EBAY_MCP_LIVE=1 to enable",
)


@pytest.fixture(scope="module")
def sandbox_ready():
    """Skip if sandbox isn't configured with usable credentials."""
    from ebay_mcp.config import load_config

    cfg = load_config()
    if "sandbox" not in cfg.hosts:
        pytest.skip("no sandbox host configured")
    try:
        cfg.resolve_cert_id("sandbox")
    except (KeyError, ValueError):
        pytest.skip("sandbox cert_id not available from env or file")
    return cfg


def test_live_app_token_acquisition(sandbox_ready):
    """First contact with sandbox: get a client_credentials token."""
    from ebay_mcp.auth import get_app_token

    token = get_app_token(sandbox_ready, "sandbox")
    assert isinstance(token, str)
    assert len(token) > 20  # eBay tokens are long


def test_live_search_executes(sandbox_ready):
    """Run a real Browse API search against the sandbox.

    Sandbox listings are sparse and inconsistent; we don't assert on hit count,
    only on response shape.
    """
    from ebay_mcp.server import search

    result = search("iphone", limit=5)
    assert result["host"] == "sandbox"
    assert "total" in result
    assert "hits" in result
    assert isinstance(result["hits"], list)
    for hit in result["hits"]:
        assert "item_id" in hit
        assert "title" in hit


def test_live_search_invalid_condition_validation_pre_network(sandbox_ready):
    """Validation runs before any HTTP call — should raise without hitting the network."""
    from ebay_mcp.server import search

    with pytest.raises(ValueError, match="condition must be one of"):
        search("anything", condition="MAYBE_USED")


def test_live_get_item_or_skip_if_no_hits(sandbox_ready):
    """Fetch a real item if search returns anything; skip otherwise.

    Sandbox often has zero results for common queries, so we tolerate that.
    """
    from ebay_mcp.server import get_item, search

    result = search("iphone", limit=1)
    if not result["hits"]:
        pytest.skip("no sandbox search hits to drill into")

    item_id = result["hits"][0]["item_id"]
    detail = get_item(item_id)
    if detail.get("missing"):
        pytest.skip(f"sandbox item {item_id} disappeared between search and get")
    assert detail["item_id"] == item_id
    assert detail["host"] == "sandbox"
