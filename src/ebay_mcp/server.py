"""MCP server entry point for ebay-mcp.

Day 1 skeleton: registers the FastMCP server and two diagnostic tools
(`server_info`, `list_hosts`) that exercise the config loader. eBay API
tools (search, watchlist, bidding, etc.) land in Day 1b onward.
"""

from __future__ import annotations

import os
import sys
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

# Money-commit safety gates. Bids/buys/offers above MONEY_CAP refuse with a
# structured payload unless the caller passes `max_bid_override >= amount`
# OR the operator has set HIGH_VALUE_OVERRIDE_ENV=1 in the environment.
# Tuned conservatively — the typical eBay-MCP workflow is sub-$100 watchlist
# bidding; anything beyond $500 should require explicit human ratification.
MONEY_CAP = 500.0
HIGH_VALUE_OVERRIDE_ENV = "EBAY_MCP_ALLOW_HIGH_VALUE"

# Trading PlaceOffer wants an EndUserIP; eBay does not validate it under the
# IAF token flow, but the field is required. A loopback constant is the
# least misleading placeholder for a non-browser caller.
PLACE_OFFER_END_USER_IP = "127.0.0.1"
DEFAULT_CURRENCY = "USD"

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


def _show_modal_confirm(title: str, body: str) -> bool:
    """Display a topmost, system-modal Yes/No dialog and block until clicked.

    Returns True for Yes, False for anything else (No, Cancel, ESC, GUI
    unavailable). No timeout; no auto-dismiss; default button is NO so a
    stray Enter-press cancels rather than confirms.

    Platform notes:
      - Windows: ctypes → user32.MessageBoxW. The Yes/No flavor has no
        working close (X) button; ESC = IDCANCEL = treated as No.
      - macOS / Linux: tkinter (stdlib). Requires a usable display.
        Headless environments return False (refuse rather than silently
        bypass).

    This is the single chokepoint for the human-in-the-loop confirmation
    on every money-commit tool. Tests stub it via
    ``monkeypatch.setattr("ebay_mcp.server._show_modal_confirm", lambda
    *_: True)``.
    """
    if sys.platform == "win32":
        try:
            import ctypes

            # MB_YESNO=0x4, MB_ICONWARNING=0x30, MB_DEFBUTTON2=0x100 (NO
            # default), MB_TOPMOST=0x40000, MB_SETFOREGROUND=0x10000.
            flags = 0x4 | 0x30 | 0x100 | 0x40000 | 0x10000
            result = ctypes.windll.user32.MessageBoxW(0, body, title, flags)
            return result == 6  # IDYES
        except Exception:
            # Never let a GUI failure auto-approve money.
            return False
    try:
        import tkinter
        from tkinter import messagebox
    except ImportError:
        return False
    try:
        root = tkinter.Tk()
        root.attributes("-topmost", True)
        root.withdraw()
        try:
            result = messagebox.askyesno(title, body, icon="warning", default="no")
        finally:
            root.destroy()
        return bool(result)
    except Exception:
        # Display unavailable (no $DISPLAY, no WindowServer, etc.). Refuse
        # rather than silently proceed without confirmation.
        return False


def _confirm_money_action(
    *,
    tool: str,
    action: str,
    item_id: str,
    amount: float,
    currency: str,
    quantity: int,
    host: str,
) -> dict[str, Any] | None:
    """Show the human-in-the-loop confirm dialog for a money-commit call.

    Returns None when the user clicks Yes (proceed); returns a structured
    refusal payload (``reason="user_declined"``) when the user clicks No
    or the dialog cannot display. The dialog is the same on sandbox and
    production hosts — muscle-memory in sandbox makes production feel
    routine, not novel — but production gets a louder header.

    There is intentionally no env-var bypass. Tests must stub
    ``_show_modal_confirm`` directly.
    """
    host_label = host.upper()
    title = f"ebay-mcp: confirm {tool} on {host_label}"
    lines = [
        f"Tool:     {tool}",
        f"Action:   {action}",
        f"Host:     {host}",
        f"Item:     {item_id}",
        f"Amount:   {amount:.2f} {currency}",
        f"Quantity: {quantity}",
        "",
        "Click YES to proceed, NO to cancel.",
    ]
    if host == "production":
        header = "*** PRODUCTION HOST — REAL MONEY ***\n\n"
    else:
        header = "Sandbox host (no real money).\n\n"
    body = header + "\n".join(lines)

    approved = _show_modal_confirm(title, body)
    if approved:
        return None
    return {
        "refused": True,
        "reason": "user_declined",
        "tool": tool,
        "host": host,
        "item_id": item_id,
        "action": action,
        "amount": amount,
        "currency": currency,
        "quantity": quantity,
        "message": (
            "User declined (or could not see) the interactive confirmation "
            "prompt. The call was not sent to eBay."
        ),
    }


def _check_money_gate(
    *,
    tool: str,
    item_id: str,
    amount: float,
    confirm_amount: float,
    max_bid_override: float | None,
    host: str,
) -> dict[str, Any] | None:
    """Run the shared safety stack for money-commit tools.

    Returns a structured refusal payload (which the calling tool returns
    directly to the LLM) when a safety gate trips, or None to clear the
    call for execution. Refusals are payloads — not exceptions — so the
    LLM can read the explanation and retry with corrected parameters.

    Gates, in order:
      1. ``confirm_amount`` must equal ``amount`` exactly. The LLM is
         expected to repeat the dollar amount; the gate proves it.
      2. ``amount`` must not exceed ``MONEY_CAP`` unless override is in
         effect. Override comes from either ``max_bid_override >= amount``
         (per-call) or HIGH_VALUE_OVERRIDE_ENV=1 (operator-wide).
      3. If ``max_bid_override`` is supplied but below ``amount`` (override
         set too low to authorize the spend), refuses.

    Empty item_id and non-positive amount/confirm are validated upstream
    via ValueError — those are programming errors, not safety-gate trips.
    """
    if confirm_amount != amount:
        return {
            "refused": True,
            "reason": "confirm_mismatch",
            "tool": tool,
            "host": host,
            "item_id": item_id,
            "amount": amount,
            "confirm_amount": confirm_amount,
            "message": (
                "Safety gate: confirm_amount must equal the bid/offer amount "
                f"exactly. Got amount={amount}, confirm_amount={confirm_amount}."
            ),
        }

    if amount > MONEY_CAP:
        env_override = os.environ.get(HIGH_VALUE_OVERRIDE_ENV) == "1"
        per_call_override_ok = (
            max_bid_override is not None and max_bid_override >= amount
        )
        if not (env_override or per_call_override_ok):
            return {
                "refused": True,
                "reason": "cap_exceeded",
                "tool": tool,
                "host": host,
                "item_id": item_id,
                "amount": amount,
                "cap": MONEY_CAP,
                "message": (
                    f"Safety gate: amount ${amount:.2f} exceeds the ${MONEY_CAP:.2f} "
                    f"per-call cap. To proceed, pass max_bid_override >= {amount} "
                    f"or set the {HIGH_VALUE_OVERRIDE_ENV}=1 environment variable "
                    "on the MCP server."
                ),
            }
        if max_bid_override is not None and max_bid_override < amount:
            # An explicit override below the spend is almost certainly a
            # typo/misunderstanding; refuse rather than silently fall back
            # to the env-var path.
            return {
                "refused": True,
                "reason": "override_too_low",
                "tool": tool,
                "host": host,
                "item_id": item_id,
                "amount": amount,
                "max_bid_override": max_bid_override,
                "message": (
                    f"Safety gate: max_bid_override ({max_bid_override}) is "
                    f"less than the amount ({amount}). The override is the "
                    "ceiling you authorize for this call; raise it to "
                    f"at least {amount} to proceed."
                ),
            }
    return None


def _place_offer(
    *,
    tool: str,
    action: str,
    item_id: str,
    amount: float,
    confirm_amount: float,
    quantity: int,
    currency: str,
    max_bid_override: float | None,
    host: str | None,
) -> dict[str, Any]:
    """Build, gate, and dispatch a Trading PlaceOffer call.

    Shared between place_bid (Action=Bid, amount→MaxBid), buy_now
    (Action=Purchase, amount→MaxBid), and make_best_offer (Action=BestOffer,
    amount→OfferPrice).
    """
    if not item_id or not item_id.strip():
        raise ValueError("item_id is required and must be non-empty")
    if amount <= 0:
        raise ValueError(f"amount must be positive; got {amount}")
    if confirm_amount <= 0:
        raise ValueError(f"confirm_amount must be positive; got {confirm_amount}")
    if quantity < 1:
        raise ValueError(f"quantity must be >= 1; got {quantity}")

    cfg = _config()
    resolved_host = cfg.resolve_host(host)

    refusal = _check_money_gate(
        tool=tool,
        item_id=item_id,
        amount=amount,
        confirm_amount=confirm_amount,
        max_bid_override=max_bid_override,
        host=resolved_host,
    )
    if refusal is not None:
        return refusal

    # Human-in-the-loop gate. Runs AFTER programmatic safety gates clear so
    # we don't pop a dialog for a call that was going to be refused anyway,
    # and BEFORE the Trading dispatch so a No-click means nothing hit eBay.
    decline = _confirm_money_action(
        tool=tool,
        action=action,
        item_id=item_id,
        amount=amount,
        currency=currency,
        quantity=quantity,
        host=resolved_host,
    )
    if decline is not None:
        return decline

    # eBay PlaceOffer uses different XML elements for the price depending
    # on Action: MaxBid for Bid/Purchase, OfferPrice for BestOffer.
    price_element = "OfferPrice" if action == "BestOffer" else "MaxBid"
    offer: dict[str, Any] = {
        "Action": action,
        price_element: {"_value": f"{amount:.2f}", "currencyID": currency},
        "Quantity": quantity,
    }
    payload = {
        "ItemID": item_id,
        "EndUserIP": PLACE_OFFER_END_USER_IP,
        "Offer": offer,
    }
    response = trading.trading_call(cfg, resolved_host, "PlaceOffer", payload=payload)

    def _price_dict(node: Any) -> dict[str, Any] | None:
        if isinstance(node, dict):
            try:
                value = float(node.get("_value") or 0) or None
            except (TypeError, ValueError):
                value = None
            return {"value": value, "currency": node.get("currencyID")}
        return None

    result: dict[str, Any] = {
        "host": resolved_host,
        "tool": tool,
        "item_id": item_id,
        "action": action,
        "amount": amount,
        "currency": currency,
        "quantity": quantity,
        "placed": True,
    }
    current_price = _price_dict(response.get("CurrentPrice"))
    if current_price:
        result["current_price"] = current_price
    minimum_to_outbid = _price_dict(response.get("MinimumToOutbid"))
    if minimum_to_outbid:
        result["minimum_to_outbid"] = minimum_to_outbid
    if "HighBidder" in response:
        result["high_bidder"] = response["HighBidder"] == "true"
    if "BestOfferID" in response:
        result["best_offer_id"] = response["BestOfferID"]
    if resolved_host == "production":
        result["warning"] = (
            "PRODUCTION HOST — this call committed real money on eBay."
        )
    return result


@mcp.tool()
def place_bid(
    item_id: str,
    max_bid_amount: float,
    confirm_amount: float,
    quantity: int = 1,
    currency: str = DEFAULT_CURRENCY,
    max_bid_override: float | None = None,
    host: str | None = None,
) -> dict[str, Any]:
    """Place a proxy bid on an eBay auction.

    Trading API PlaceOffer with Action=Bid. eBay treats max_bid_amount as
    a proxy ceiling — it bids the minimum needed to outbid the current
    high bidder, and continues raising up to max_bid_amount as competing
    bids come in.

    PRODUCTION HOST WARNING: when the active host is "production", a
    successful call commits real money on eBay. Inspect server_info()
    or list_hosts() before committing.

    Args:
        item_id: numeric eBay item ID. Get from search() or get_item().
        max_bid_amount: the proxy bid ceiling in `currency`. Must be > 0.
        confirm_amount: must equal max_bid_amount exactly. The repeated
            dollar amount is a safety gate against typos.
        quantity: number of units (default 1; only relevant for
            multi-quantity auctions).
        currency: ISO currency code matching the listing (default "USD").
            Must match the listing's currency or eBay refuses.
        max_bid_override: optional ceiling that authorizes amounts above
            the $500 per-call safety cap. Pass a value >= max_bid_amount
            to bypass the cap for this call. Lower values are refused.
        host: configured host name. Defaults to default_host. The active
            host (sandbox vs production) is surfaced in the response.

    Returns:
        On success: dict with host, item_id, action="Bid", amount,
        currency, placed=True, current_price, minimum_to_outbid,
        high_bidder, and (on production) a warning field.
        On safety-gate failure: structured refusal payload (refused=True,
        reason, message, plus the offending values).
        Raises ValueError on empty item_id, non-positive amounts, etc.
        On eBay-side errors (listing ended, currency mismatch, insufficient
        bid, etc.) raises TradingApiError.
    """
    return _place_offer(
        tool="place_bid",
        action="Bid",
        item_id=item_id,
        amount=max_bid_amount,
        confirm_amount=confirm_amount,
        quantity=quantity,
        currency=currency,
        max_bid_override=max_bid_override,
        host=host,
    )


@mcp.tool()
def buy_now(
    item_id: str,
    confirm_amount: float,
    quantity: int = 1,
    currency: str = DEFAULT_CURRENCY,
    max_bid_override: float | None = None,
    host: str | None = None,
) -> dict[str, Any]:
    """Purchase a Buy It Now listing immediately at the listed price.

    Trading API PlaceOffer with Action=Purchase. The buyer commits to
    pay the listing's BIN price; eBay creates the order on success.

    PRODUCTION HOST WARNING: when the active host is "production", a
    successful call commits real money on eBay. Inspect server_info()
    or list_hosts() before committing.

    Args:
        item_id: numeric eBay item ID. Get from search() or get_item().
        confirm_amount: must equal the listing's Buy-It-Now price
            exactly. The repeated dollar amount is a safety gate against
            stale prices or typos. Look up the current price with
            get_item() right before calling.
        quantity: number of units to purchase (default 1). Multi-quantity
            BIN listings can have a per-buyer limit; eBay rejects with
            TradingApiError if violated.
        currency: ISO currency code matching the listing (default "USD").
        max_bid_override: optional ceiling that authorizes amounts above
            the $500 per-call safety cap. Pass a value >= confirm_amount
            to bypass the cap for this call.
        host: configured host name. Defaults to default_host.

    Returns:
        On success: dict with host, item_id, action="Purchase", amount,
        currency, quantity, placed=True, and (on production) a warning.
        On safety-gate failure: structured refusal payload.
        Raises ValueError / TradingApiError as in place_bid.
    """
    return _place_offer(
        tool="buy_now",
        action="Purchase",
        item_id=item_id,
        amount=confirm_amount,
        confirm_amount=confirm_amount,
        quantity=quantity,
        currency=currency,
        max_bid_override=max_bid_override,
        host=host,
    )


@mcp.tool()
def make_best_offer(
    item_id: str,
    offer_amount: float,
    confirm_amount: float,
    quantity: int = 1,
    currency: str = DEFAULT_CURRENCY,
    max_bid_override: float | None = None,
    host: str | None = None,
) -> dict[str, Any]:
    """Submit a Best Offer on a listing that has Best Offer enabled.

    Trading API PlaceOffer with Action=BestOffer. The seller can accept,
    counter, or decline; this call only places the offer.

    PRODUCTION HOST WARNING: when the active host is "production" and
    the seller accepts, the offer becomes a binding sale at the offer
    amount.

    Args:
        item_id: numeric eBay item ID. Must have Best Offer enabled —
            check `buying_options` on get_item() output for "BEST_OFFER".
        offer_amount: the price the buyer offers, in `currency`. Must be
            > 0. Many sellers configure auto-decline below a threshold.
        confirm_amount: must equal offer_amount exactly. Safety gate.
        quantity: number of units the offer covers (default 1).
        currency: ISO currency code matching the listing (default "USD").
        max_bid_override: optional ceiling that authorizes offer amounts
            above the $500 per-call safety cap. Pass a value >=
            offer_amount to bypass.
        host: configured host name. Defaults to default_host.

    Returns:
        On success: dict with host, item_id, action="BestOffer",
        amount, currency, quantity, placed=True, optionally
        best_offer_id (for follow-up via Trading GetBestOffer / Accept
        flows), and (on production) a warning.
        On safety-gate failure: structured refusal payload.
        Raises ValueError / TradingApiError as in place_bid.
    """
    return _place_offer(
        tool="make_best_offer",
        action="BestOffer",
        item_id=item_id,
        amount=offer_amount,
        confirm_amount=confirm_amount,
        quantity=quantity,
        currency=currency,
        max_bid_override=max_bid_override,
        host=host,
    )


def main() -> None:
    """Console-script entry point. Runs the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
