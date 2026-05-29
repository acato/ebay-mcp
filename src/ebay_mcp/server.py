"""MCP server entry point for ebay-mcp.

Day 1 skeleton: registers the FastMCP server and two diagnostic tools
(`server_info`, `list_hosts`) that exercise the config loader. eBay API
tools (search, watchlist, bidding, etc.) land in Day 1b onward.
"""

from __future__ import annotations

from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from ebay_mcp import __version__, auth, trading
from ebay_mcp.auth import get_app_token
from ebay_mcp.config import Config, config_path, load_config
from ebay_mcp.urls import urls_for_host

mcp = FastMCP("ebay-mcp")

# Hard caps that keep individual tool responses tractable for the LLM.
SEARCH_LIMIT_MAX = 200
DEFAULT_SEARCH_LIMIT = 20

# Map LLM-facing sort names → eBay Browse API sort parameter values.
_SORT_MAP: dict[str, str | None] = {
    "best_match": None,  # eBay default, no sort param
    "price_asc": "price",
    "price_desc": "-price",
    "newly_listed": "newlyListed",
    "ending_soonest": "endingSoonest",
}

# Valid Browse API condition strings. eBay rejects anything else.
VALID_CONDITIONS = {
    "NEW",
    "LIKE_NEW",
    "NEW_OTHER",
    "NEW_WITH_DEFECTS",
    "MANUFACTURER_REFURBISHED",
    "CERTIFIED_REFURBISHED",
    "EXCELLENT_REFURBISHED",
    "VERY_GOOD_REFURBISHED",
    "GOOD_REFURBISHED",
    "SELLER_REFURBISHED",
    "USED_EXCELLENT",
    "USED_VERY_GOOD",
    "USED_GOOD",
    "USED_ACCEPTABLE",
    "FOR_PARTS_OR_NOT_WORKING",
}


def _config() -> Config:
    """Load fresh config on each call so file edits don't require a server restart."""
    return load_config()


@mcp.tool()
def server_info() -> dict[str, Any]:
    """Return server version, active host, and configuration locations.

    The `active_host` field reflects what `default_host` is set to (or what
    would be used when a tool call omits the `host` parameter). When set to
    "production", the response includes a `warning` field — real money is
    at stake on bid/buy operations.
    """
    cfg = _config()
    cfg_path = config_path()
    out: dict[str, Any] = {
        "version": __version__,
        "config_path": str(cfg_path),
        "config_exists": str(cfg_path.exists()),
        "active_host": cfg.default_host,
        "configured_hosts": sorted(cfg.hosts.keys()),
    }
    if cfg.default_host == "production":
        out["warning"] = "PRODUCTION HOST ACTIVE — bid/buy/offer operations will commit REAL money"
    return out


@mcp.tool()
def list_hosts() -> list[dict[str, Any]]:
    """List every configured host with its status (default flag and cert availability).

    Useful for diagnosing why a tool call against a particular host might fail
    before you make the call.
    """
    cfg = _config()
    out: list[dict[str, Any]] = []
    for name in sorted(cfg.hosts.keys()):
        host = cfg.hosts[name]
        # Check cert availability without raising
        import os

        env_var = f"EBAY_MCP_{name.upper()}_CERT_ID"
        cert_in_env = bool(os.environ.get(env_var))
        cert_in_file = bool(host.cert_id)
        out.append(
            {
                "name": name,
                "is_default": name == cfg.default_host,
                "app_id_set": bool(host.app_id),
                "dev_id_set": bool(host.dev_id),
                "cert_id_in_env": cert_in_env,
                "cert_id_in_file": cert_in_file,
                "credentials_ready": bool(
                    host.app_id and host.dev_id and (cert_in_env or cert_in_file)
                ),
                "redirect_uri": host.redirect_uri,
            }
        )
    return out


def _build_filter(
    *,
    condition: str | None,
    min_price: float | None,
    max_price: float | None,
    currency: str,
) -> str | None:
    """Build the eBay Browse API `filter` query-string value, or None if no filters."""
    parts: list[str] = []
    if condition:
        parts.append(f"conditions:{{{condition}}}")
    if min_price is not None or max_price is not None:
        lo = f"{min_price:g}" if min_price is not None else ""
        hi = f"{max_price:g}" if max_price is not None else ""
        # Bracket range syntax: [lo..hi]
        parts.append(f"price:[{lo}..{hi}]")
        parts.append(f"priceCurrency:{currency}")
    return ",".join(parts) if parts else None


def _normalize_item_summary(raw: dict[str, Any]) -> dict[str, Any]:
    """Project an eBay item-summary dict to a smaller LLM-facing shape."""
    price = raw.get("price") or {}
    seller = raw.get("seller") or {}
    image = raw.get("image") or {}
    out: dict[str, Any] = {
        "item_id": raw.get("itemId", ""),
        "title": raw.get("title", ""),
        "price": float(price["value"]) if price.get("value") is not None else None,
        "currency": price.get("currency"),
        "condition": raw.get("condition"),
        "seller": seller.get("username"),
        "seller_feedback_score": seller.get("feedbackScore"),
        "image_url": image.get("imageUrl"),
        "ends_at": raw.get("itemEndDate"),
        "buying_options": raw.get("buyingOptions", []),
        "web_url": raw.get("itemWebUrl"),
    }
    # `bidCount` only present on auctions; surface when set.
    if "bidCount" in raw:
        out["bid_count"] = raw["bidCount"]
    return out


def _normalize_item_detail(raw: dict[str, Any]) -> dict[str, Any]:
    """Project a full Browse API item dict to the LLM shape; mostly a superset of summary."""
    out = _normalize_item_summary(raw)
    out["description"] = raw.get("shortDescription") or raw.get("description")
    seller = raw.get("seller") or {}
    if seller.get("feedbackPercentage") is not None:
        out["seller_feedback_percentage"] = seller["feedbackPercentage"]
    if "estimatedAvailabilities" in raw:
        out["estimated_availabilities"] = raw["estimatedAvailabilities"]
    if "shippingOptions" in raw:
        out["shipping_options"] = raw["shippingOptions"]
    if "returnTerms" in raw:
        out["return_terms"] = raw["returnTerms"]
    if "itemLocation" in raw:
        out["item_location"] = raw["itemLocation"]
    return out


def _browse_get(
    url: str, token: str, *, params: dict[str, Any] | None = None, marketplace: str = "EBAY_US"
) -> httpx.Response:
    """Issue a Browse API GET with the standard auth + marketplace headers."""
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": marketplace,
        "Accept": "application/json",
    }
    return httpx.get(url, headers=headers, params=params, timeout=30)


@mcp.tool()
def search(
    query: str,
    category_id: str | None = None,
    condition: str | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    currency: str = "USD",
    sort: str = "best_match",
    limit: int = DEFAULT_SEARCH_LIMIT,
    offset: int = 0,
    marketplace: str = "EBAY_US",
    host: str | None = None,
) -> dict[str, Any]:
    """Search eBay items via the Browse API.

    Args:
        query: keyword search string. Required.
        category_id: numeric eBay category ID to restrict the search.
        condition: one of NEW, LIKE_NEW, NEW_OTHER, USED_EXCELLENT,
            USED_VERY_GOOD, USED_GOOD, USED_ACCEPTABLE, FOR_PARTS_OR_NOT_WORKING,
            MANUFACTURER_REFURBISHED, CERTIFIED_REFURBISHED, or several other
            refurbished tiers. Case-sensitive.
        min_price: minimum item price (in `currency`).
        max_price: maximum item price (in `currency`).
        currency: ISO currency code for the price filter (default "USD").
        sort: one of "best_match" (default), "price_asc", "price_desc",
            "newly_listed", "ending_soonest".
        limit: number of hits per page. Default 20, hard cap 200.
        offset: pagination offset.
        marketplace: eBay marketplace ID (default "EBAY_US"). Other examples:
            "EBAY_GB", "EBAY_DE", "EBAY_IT".
        host: which configured host to use ("sandbox" or "production"). If
            omitted, uses the config's `default_host`.

    Returns:
        dict with:
          - host: which host was actually targeted
          - total: total matching items per eBay
          - limit, offset, next_offset (None when there's no next page)
          - hits: list of item summaries (item_id, title, price, currency,
            condition, seller, image_url, ends_at, buying_options, web_url,
            bid_count if applicable)
    """
    if not query or not query.strip():
        raise ValueError("query is required and must be non-empty")
    if limit < 1 or limit > SEARCH_LIMIT_MAX:
        raise ValueError(f"limit must be between 1 and {SEARCH_LIMIT_MAX}")
    if offset < 0:
        raise ValueError("offset must be >= 0")
    if sort not in _SORT_MAP:
        raise ValueError(f"sort must be one of {sorted(_SORT_MAP)}; got {sort!r}")
    if condition is not None and condition not in VALID_CONDITIONS:
        raise ValueError(f"condition must be one of {sorted(VALID_CONDITIONS)}; got {condition!r}")

    cfg = _config()
    resolved_host = cfg.resolve_host(host)
    token = get_app_token(cfg, resolved_host)
    urls = urls_for_host(resolved_host)

    params: dict[str, Any] = {"q": query, "limit": limit, "offset": offset}
    if category_id:
        params["category_ids"] = category_id
    filt = _build_filter(
        condition=condition, min_price=min_price, max_price=max_price, currency=currency
    )
    if filt:
        params["filter"] = filt
    if _SORT_MAP[sort]:
        params["sort"] = _SORT_MAP[sort]

    response = _browse_get(
        f"{urls['browse']}/item_summary/search",
        token,
        params=params,
        marketplace=marketplace,
    )
    response.raise_for_status()
    body = response.json()

    total = int(body.get("total", 0))
    hits = [_normalize_item_summary(it) for it in body.get("itemSummaries", [])]
    next_offset = offset + limit if offset + limit < total else None

    return {
        "host": resolved_host,
        "total": total,
        "limit": limit,
        "offset": offset,
        "next_offset": next_offset,
        "hits": hits,
    }


@mcp.tool()
def get_item(item_id: str, marketplace: str = "EBAY_US", host: str | None = None) -> dict[str, Any]:
    """Fetch full details for a single eBay item by ID.

    Args:
        item_id: the eBay item ID (e.g., "v1|353528728623|0"). Get these from
            search() results.
        marketplace: eBay marketplace ID (default "EBAY_US").
        host: which configured host to use. Defaults to config's `default_host`.

    Returns:
        Full item dict including description, shipping options, seller details,
        return terms, item location, and the standard summary fields.
        If the item doesn't exist or has ended, returns
        {"item_id": item_id, "host": host, "missing": true}.
    """
    if not item_id or not item_id.strip():
        raise ValueError("item_id is required and must be non-empty")

    cfg = _config()
    resolved_host = cfg.resolve_host(host)
    token = get_app_token(cfg, resolved_host)
    urls = urls_for_host(resolved_host)

    response = _browse_get(
        f"{urls['browse']}/item/{item_id}",
        token,
        marketplace=marketplace,
    )
    if response.status_code == 404:
        return {"item_id": item_id, "host": resolved_host, "missing": True}
    response.raise_for_status()
    body = response.json()
    out = _normalize_item_detail(body)
    out["host"] = resolved_host
    return out


def _normalize_trading_item(raw: dict[str, Any]) -> dict[str, Any]:
    """Project a Trading API <Item> dict to the LLM-facing summary shape.

    Mirrors the keys we use for Browse API search hits where they overlap
    (item_id, title, price, currency, ends_at, web_url, bid_count, seller),
    so an LLM consuming both Browse and Trading results sees a consistent
    surface.
    """
    selling_status = raw.get("SellingStatus") or {}
    current_price = selling_status.get("CurrentPrice") or {}
    # CurrentPrice element is `<CurrentPrice currencyID="USD">9.99</CurrentPrice>`
    # which our parser maps to {"_value": "9.99", "currencyID": "USD"}.
    if isinstance(current_price, dict):
        try:
            price: float | None = float(current_price.get("_value") or 0) or None
        except (TypeError, ValueError):
            price = None
        currency = current_price.get("currencyID")
    else:
        price = None
        currency = None

    seller = raw.get("Seller") or {}

    def _to_int(value: Any) -> int | None:
        try:
            return int(value) if value not in (None, "", {}) else None
        except (TypeError, ValueError):
            return None

    out: dict[str, Any] = {
        "item_id": raw.get("ItemID") or "",
        "title": raw.get("Title") or "",
        "price": price,
        "currency": currency,
        "ends_at": raw.get("EndTime"),
        "bid_count": _to_int(selling_status.get("BidCount")),
        "seller": seller.get("UserID") if isinstance(seller, dict) else None,
        "web_url": raw.get("ViewItemURL") or "",
        "listing_type": raw.get("ListingType"),
        "quantity_available": _to_int(raw.get("Quantity")),
    }
    # WonList items carry purchase-transaction metadata (paid/shipped/status)
    # that the upstream extractor tagged onto the raw dict. Surface a flat
    # subset on the envelope.
    txn = raw.get("_transaction")
    if isinstance(txn, dict):
        out["purchase_date"] = txn.get("created_date")
        out["paid_at"] = txn.get("paid_time")
        out["shipped_at"] = txn.get("shipped_time")
        out["transaction_status"] = txn.get("status")
        out["buyer_paid_status"] = txn.get("buyer_paid_status")
        out["quantity_purchased"] = _to_int(txn.get("quantity_purchased"))
        out["transaction_id"] = txn.get("transaction_id")
    return out


def _extract_items_from_container(container: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull item dicts out of a GetMyeBayBuying container.

    Two layouts exist:
      - **ItemArray > Item** (WatchList, BidList, LostList): straight item rows.
      - **OrderTransactionArray > OrderTransaction > Transaction > Item**
        (WonList): items wrapped in purchase-transaction metadata. We unwrap
        and tag the item with the surrounding transaction fields (purchase
        date, paid/shipped times, transaction status) so the normalized
        envelope can surface them.
    """
    item_array = container.get("ItemArray") or {}
    raw = item_array.get("Item")
    if raw:
        return [raw] if isinstance(raw, dict) else list(raw)

    ot_array = container.get("OrderTransactionArray") or {}
    raw_ots = ot_array.get("OrderTransaction") or []
    if isinstance(raw_ots, dict):
        raw_ots = [raw_ots]

    items: list[dict[str, Any]] = []
    for ot in raw_ots:
        txn = ot.get("Transaction") or {}
        item = txn.get("Item")
        if not isinstance(item, dict):
            continue
        # Copy so we don't mutate the parsed response; tag with txn metadata.
        annotated = dict(item)
        annotated["_transaction"] = {
            "transaction_id": txn.get("TransactionID"),
            "created_date": txn.get("CreatedDate"),
            "paid_time": txn.get("PaidTime"),
            "shipped_time": txn.get("ShippedTime"),
            "status": txn.get("Status"),
            "buyer_paid_status": txn.get("BuyerPaidStatus"),
            "quantity_purchased": txn.get("QuantityPurchased"),
        }
        items.append(annotated)
    return items


def _get_mybuying_container(
    container_name: str,
    limit: int,
    offset: int,
    host: str | None,
    *,
    extra_container_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Shared logic for the four GetMyeBayBuying containers.

    container_name: "WatchList" | "BidList" | "WonList" | "LostList"
    """
    if limit < 1 or limit > 200:
        raise ValueError("limit must be between 1 and 200")
    if offset < 0:
        raise ValueError("offset must be >= 0")

    cfg = _config()
    resolved_host = cfg.resolve_host(host)

    # Trading API uses 1-based page numbers, not offsets. Convert.
    page_number = (offset // limit) + 1
    container_payload: dict[str, Any] = {
        "Include": True,
        "Pagination": {
            "EntriesPerPage": limit,
            "PageNumber": page_number,
        },
    }
    if extra_container_fields:
        container_payload.update(extra_container_fields)

    response = trading.trading_call(
        cfg,
        resolved_host,
        "GetMyeBayBuying",
        payload={container_name: container_payload},
    )

    container = response.get(container_name) or {}
    raw_items = _extract_items_from_container(container)

    pagination = container.get("PaginationResult") or {}
    try:
        total = int(pagination.get("TotalNumberOfEntries", "0"))
    except (TypeError, ValueError):
        total = len(raw_items)

    hits = [_normalize_trading_item(item) for item in raw_items]

    return {
        "host": resolved_host,
        "container": container_name,
        "total": total,
        "limit": limit,
        "offset": offset,
        "hits": hits,
    }


@mcp.tool()
def get_watchlist(
    limit: int = 100,
    offset: int = 0,
    host: str | None = None,
) -> dict[str, Any]:
    """Return the authenticated user's watched items.

    Uses Trading API `GetMyeBayBuying` with the WatchList container.
    Requires that the user has authenticated via `start_user_auth` +
    `complete_user_auth` for the chosen host.

    Args:
        limit: items per page (1-200). Default 100.
        offset: pagination offset. Trading API pages from 1; we translate.
        host: configured host name. Defaults to default_host.

    Returns:
        dict with `host`, `container`, `total`, `limit`, `offset`, and
        `hits` (a list of item summaries: item_id, title, price, currency,
        ends_at, bid_count, seller, web_url, listing_type,
        quantity_available).
    """
    return _get_mybuying_container("WatchList", limit, offset, host)


@mcp.tool()
def get_active_bids(
    limit: int = 100,
    offset: int = 0,
    host: str | None = None,
) -> dict[str, Any]:
    """Return auctions where the user has a currently-active bid.

    Trading API GetMyeBayBuying.BidList. Includes both winning and
    outbid items still in their bidding period.

    Same response shape as `get_watchlist`. Empty hits + total=0 means
    you have no active bids.
    """
    return _get_mybuying_container("BidList", limit, offset, host)


@mcp.tool()
def get_won_items(
    limit: int = 100,
    offset: int = 0,
    host: str | None = None,
) -> dict[str, Any]:
    """Return items the user won at auction or bought via Buy It Now.

    Trading API GetMyeBayBuying.WonList. This is your effective
    "purchase history" for the eBay-native default lookback window
    (eBay shows the last 30 days by default; older items roll off
    the WonList container).

    Same response shape as `get_watchlist`.
    """
    return _get_mybuying_container("WonList", limit, offset, host)


@mcp.tool()
def get_lost_items(
    limit: int = 100,
    offset: int = 0,
    host: str | None = None,
) -> dict[str, Any]:
    """Return auctions where the user bid but did not win.

    Trading API GetMyeBayBuying.LostList. Default lookback ~30 days;
    older items roll off.

    Same response shape as `get_watchlist`.
    """
    return _get_mybuying_container("LostList", limit, offset, host)


@mcp.tool()
def add_to_watchlist(item_id: str, host: str | None = None) -> dict[str, Any]:
    """Add an item to the authenticated user's watchlist.

    Trading API `AddToWatchList`. Fully reversible — call
    `remove_from_watchlist` with the same item_id to undo.

    Args:
        item_id: numeric eBay item ID (from `search`, `get_item`, etc.)
        host: configured host. Defaults to default_host.

    Returns:
        dict with `host`, `item_id`, `added: True`, and
        `watch_list_count` (total items in the watchlist after the
        operation). Raises ValueError on empty item_id.
        On eBay-side errors (item ended, already watched, etc.) the
        Trading API raises TradingApiError with details.
    """
    if not item_id or not item_id.strip():
        raise ValueError("item_id is required and must be non-empty")

    cfg = _config()
    resolved_host = cfg.resolve_host(host)
    response = trading.trading_call(
        cfg, resolved_host, "AddToWatchList", payload={"ItemID": item_id}
    )

    raw_count = response.get("WatchListCount")
    if not raw_count:
        count = -1
    else:
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            count = -1
    return {
        "host": resolved_host,
        "item_id": item_id,
        "added": True,
        "watch_list_count": count,
    }


@mcp.tool()
def remove_from_watchlist(item_id: str, host: str | None = None) -> dict[str, Any]:
    """Remove an item from the authenticated user's watchlist.

    Trading API `RemoveFromWatchList`. Fully reversible — call
    `add_to_watchlist` with the same item_id to undo.

    Args:
        item_id: numeric eBay item ID currently on the watchlist.
        host: configured host. Defaults to default_host.

    Returns:
        dict with `host`, `item_id`, `removed: True`, and
        `watch_list_count` (total items in the watchlist after the
        operation). Raises ValueError on empty item_id. eBay-side
        errors (item not on watchlist, etc.) surface as TradingApiError.
    """
    if not item_id or not item_id.strip():
        raise ValueError("item_id is required and must be non-empty")

    cfg = _config()
    resolved_host = cfg.resolve_host(host)
    response = trading.trading_call(
        cfg, resolved_host, "RemoveFromWatchList", payload={"ItemID": item_id}
    )

    raw_count = response.get("WatchListCount")
    if not raw_count:
        count = -1
    else:
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            count = -1
    return {
        "host": resolved_host,
        "item_id": item_id,
        "removed": True,
        "watch_list_count": count,
    }


@mcp.tool()
def start_user_auth(host: str | None = None) -> dict[str, Any]:
    """Begin OAuth2 user authentication for buyer-side eBay operations.

    Watchlist, MyeBay reads, bidding, and buying all require a user token.
    This tool returns an `auth_url` for you (the human) to open in a browser.
    After signing in and granting permissions, eBay redirects to the
    configured `redirect_uri` with a `code` query parameter — pass that to
    `complete_user_auth` to finalize.

    Most workflows only run this once per host; tokens persist 18 months
    and auto-refresh.

    Args:
        host: which configured host to authenticate against. Defaults to
            config's `default_host`.

    Returns:
        dict with `host`, `auth_url`, `state`, and `instructions`. The
        `auth_url` is what the human needs to open; the `state` is
        round-tripped to verify the callback genuinely came from this flow.
    """
    cfg = _config()
    resolved_host = cfg.resolve_host(host)
    return auth.start_user_auth(cfg, resolved_host)


@mcp.tool()
def complete_user_auth(
    code: str, state: str | None = None, host: str | None = None
) -> dict[str, Any]:
    """Finish OAuth2 user auth by exchanging the authorization code for tokens.

    The `code` is the value of the `code` query parameter in the URL eBay
    redirected to after start_user_auth's `auth_url`. Optionally pass `state`
    to verify against what start_user_auth issued (CSRF defense).

    Args:
        code: the authorization code from eBay's redirect URL.
        state: optional — the state value from the redirect URL. If provided,
            must match what start_user_auth stored. Mismatch returns a
            structured refusal payload.
        host: which configured host to authenticate against. Defaults to
            config's `default_host`. Must match the host used in
            start_user_auth.

    Returns:
        On success: {host, authenticated: true, access_expires_at,
                     refresh_expires_at}
        On state mismatch: {refused: true, reason: "state_mismatch", host,
                            message: ...}
    """
    cfg = _config()
    resolved_host = cfg.resolve_host(host)
    return auth.complete_user_auth(cfg, resolved_host, code, state=state)


def main() -> None:
    """Console-script entry point. Runs the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
