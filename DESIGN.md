# DESIGN.md — ebay-mcp

## 1. Goals and non-goals

**Goal.** Make eBay's buyer-side workflows reachable from an MCP-using LLM with strict safety guarantees on the money-commit subset. Search, watch, MyeBay management, plus bid / buy / best-offer with confirm-amount gating and per-call dollar cap.

**Non-goals (deliberate, do not slowly creep into these):**
- Not a selling-side MCP. eBay's Sell API is a different shape entirely; users wanting that should build a separate `ebay-sell-mcp` if needed.
- Not a snipe-bidding scheduler. One tool call = one bid. The LLM (or a higher-level orchestrator) handles timing.
- Not a multi-account aggregator. v0 supports one user account per host config (sandbox + production). Multi-user can come later.
- Not a Playwright wrapper. We use real APIs only. If a feature is only reachable via ebay.com UI, it's out of scope.

## 2. API stack

eBay's API surface in 2026 is bifurcated:

| Capability | API | Style | Why |
|---|---|---|---|
| Search items | Browse API | REST/JSON, OAuth2 | Modern, clean. Public scope; uses client-credentials token. |
| Get item details | Browse API | REST/JSON, OAuth2 | Same. |
| Place bid / buy / best offer | Trading API | SOAP/XML, OAuth2 user token | First-party REST bidding APIs were deprecated/removed. Trading API still works for individual developer keys via the standard Auth'n'Auth or OAuth2 user-token flow with `PlaceOffer` scope. |
| Watchlist read/write | Trading API | SOAP/XML, OAuth2 user token | Trading `GetMyeBayBuying`, `AddToWatchList`, `RemoveFromWatchList`. The Buy Marketplace Insights API has read-side watchlist support but no write. |
| MyeBay reads (active bids, won, lost, purchases) | Trading API | SOAP/XML, OAuth2 user token | `GetMyeBayBuying` with appropriate detail-level filters. |

**Hybrid architecture, single auth layer.** Both APIs ride on the same OAuth2 user token (or app token for Browse public scopes). `httpx` for both; for Trading we hand-roll the XML envelope using stdlib `xml.etree.ElementTree` rather than pull in `lxml` — keeps the dependency tree minimal.

## 3. Authentication

### Browse API — app-level OAuth2 client_credentials

Used for public search and item-detail lookup. No user context needed.

1. POST to `<oauth_url>` with Basic Auth = base64(app_id:cert_id), body `grant_type=client_credentials&scope=https://api.ebay.com/oauth/api_scope`
2. Cache the returned access_token (typically 7200s lifetime)
3. Refresh proactively at 75% lifetime; full retry on 401

### Trading API + user-scoped Buy operations — OAuth2 authorization_code

User authorization is interactive but only required once per host (token persists 18 months; refresh tokens last 18 months and auto-renew).

**Manual flow (no callback server required):**

1. MCP exposes `start_user_auth(host)` which prints an authorization URL + a marker. URL constructed from `<auth_authorize_url>` + `client_id=<app_id>` + `redirect_uri=<configured>` + `scope=<space-separated user scopes>` + `state=<random>`.
2. User opens URL in browser, signs in, grants permissions.
3. eBay redirects to `<redirect_uri>?code=<auth_code>&state=<state>`. The redirect URI doesn't have to be a real server — the user copies the `code` from the URL bar.
4. User calls `complete_user_auth(host, auth_code)` with the code.
5. MCP exchanges the code for access_token (2hr) + refresh_token (18mo), caches both to `~/.config/ebay-mcp/token-cache-<host>.json` (chmod 600 best-effort on Windows).
6. Subsequent calls auto-refresh access_token from refresh_token on 401.

### Scopes requested

For the v0 tool surface:
- `https://api.ebay.com/oauth/api_scope` — public Browse
- `https://api.ebay.com/oauth/api_scope/buy.item.feed` — item feed access
- `https://api.ebay.com/oauth/api_scope/buy.marketing` — marketing
- `https://api.ebay.com/oauth/api_scope/buy.order.readonly` — order history
- Trading API uses separate "eAuth" tokens via the Trading API user flow; cached separately in the same file under a different key

## 4. Tool surface (v0 MVP)

All tools take an optional `host` parameter. If omitted, uses `default_host` from config. Refusal messages always state which host was actually targeted.

### Search / read

| Tool | Args | Returns |
|---|---|---|
| `search` | `query`, `category?`, `condition?`, `min_price?`, `max_price?`, `sort="best_match"\|"price_asc"\|"price_desc"\|"newly_listed"\|"ending_soonest"`, `limit=20`, `offset=0`, `host?` | `{total, hits: [{item_id, title, price, condition, seller, ends_at, image_url, web_link}]}` |
| `get_item` | `item_id`, `host?` | Full item dict with description, shipping, seller details, current bid, time-left |
| `get_watchlist` | `host?` | `[{item_id, title, current_price, time_left, ...}]` |
| `get_active_bids` | `host?` | Items currently bid on by the user |
| `get_won_items` | `limit=50`, `offset=0`, `host?` | Items the user won |
| `get_lost_items` | `limit=50`, `offset=0`, `host?` | Items the user bid on but lost |
| `get_purchase_history` | `limit=50`, `offset=0`, `host?` | Completed purchases |

### Watchlist writes

| Tool | Args | Returns |
|---|---|---|
| `add_to_watchlist` | `item_id`, `host?` | `{watching: true, item_id}` |
| `remove_from_watchlist` | `item_id`, `host?` | `{watching: false, item_id}` |

### Money commits

| Tool | Args | Returns |
|---|---|---|
| `place_bid` | `item_id`, `max_bid_amount`, `confirm_amount`, `currency="USD"`, `max_bid_override=False`, `host?` | `{placed: true, item_id, bid_amount, host}` OR structured refusal |
| `buy_now` | `item_id`, `confirm_amount`, `quantity=1`, `currency="USD"`, `max_bid_override=False`, `host?` | `{purchased: true, item_id, total, host}` OR refusal |
| `make_best_offer` | `item_id`, `offer_amount`, `confirm_amount`, `currency="USD"`, `max_bid_override=False`, `host?` | `{offered: true, item_id, amount, host}` OR refusal |

**Refusal payload shape** (matches `yahoo-mail-mcp`'s `bulk_purge_from`):

```json
{
  "refused": true,
  "reason": "confirm_mismatch" | "exceeds_cap" | "auth_missing" | "item_ended",
  "host": "sandbox",
  "expected": 42.00,
  "actual": 50.00,
  "message": "..."
}
```

### Diagnostics

| Tool | Returns |
|---|---|
| `server_info` | version, active host (LOUDLY surfaces "production" when active), config path |
| `list_hosts` | All configured hosts with status (auth ok / needs reauth / not configured) |

## 5. Safety patterns (must implement before any money-commit tool ships)

1. **Confirm-amount must equal the value-at-risk exactly.** `place_bid(max_bid_amount=42, confirm_amount=42)` proceeds; `place_bid(42, 41)` refuses. Mirrors `bulk_purge_from`'s `confirm_count` mechanism.
2. **Per-call dollar cap = $500** for bids/buys/offers. Higher amounts refuse unless `max_bid_override=True` is explicitly passed (per-call) OR `EBAY_MCP_ALLOW_HIGH_VALUE=1` is set (global; less recommended).
3. **Host transparency.** Every money-commit response includes `host`. Every money-commit tool's docstring says: "running against PRODUCTION will commit real money".
4. **`server_info` surfaces active host loudly.** `production` → response includes `warning: "PRODUCTION HOST ACTIVE — real money operations will hit production"`.
5. **Auth-missing refusal** if user token expired and refresh failed. Don't attempt the operation half-blind.
6. **No silent success on partial failure.** If Trading API returns `Success` with warnings (e.g., bid placed but already outbid), surface the warning.
7. **Auction-ended refusal.** If `get_item` returns `time_left = 0` or `bid_count` indicates the auction is over, `place_bid` returns refusal with `reason: "item_ended"` without calling Trading API.

## 6. Configuration

**Default location:** `~/.config/ebay-mcp/config.toml`
**Override:** `EBAY_MCP_CONFIG=/absolute/path`

```toml
default_host = "sandbox"

[hosts.sandbox]
app_id = "..."
dev_id = "..."
cert_id = "..."             # env override: EBAY_MCP_SANDBOX_CERT_ID
redirect_uri = "https://localhost/oauth/callback"

[hosts.production]
app_id = "..."
dev_id = "..."
cert_id = "..."             # env override: EBAY_MCP_PRODUCTION_CERT_ID
redirect_uri = "https://localhost/oauth/callback"
```

**URL constants are hardcoded by host name** in `urls.py` — they're deterministic per environment and there's no benefit to making them configurable:

```python
SANDBOX_URLS = {
    "browse": "https://api.sandbox.ebay.com/buy/browse/v1",
    "trading": "https://api.sandbox.ebay.com/ws/api.dll",
    "oauth_token": "https://api.sandbox.ebay.com/identity/v1/oauth2/token",
    "oauth_authorize": "https://auth.sandbox.ebay.com/oauth2/authorize",
}
PRODUCTION_URLS = { ... }  # production equivalents
```

## 7. Token cache

`~/.config/ebay-mcp/token-cache-<host>.json`:

```json
{
  "client_credentials": {
    "access_token": "...",
    "expires_at": "2026-05-28T22:00:00Z"
  },
  "user": {
    "access_token": "...",
    "refresh_token": "...",
    "access_expires_at": "...",
    "refresh_expires_at": "...",
    "user_id": "sandbox-test-user-xyz"
  }
}
```

Auto-refresh on every tool call that needs a token. File is gitignored; chmod 600 best-effort.

## 8. Error model

Every tool returns either:
- Success: typed result per schema
- Refusal (expected, structured): `{refused: true, reason: "...", ...}` — LLM can recover
- Error (exceptional): `{error: "...", message: "...", retryable: bool}` — surface to user

Refusal vs error split: anything the LLM can fix by changing inputs is refusal. Anything that needs the user to intervene (auth, server down, eBay API outage) is error.

## 9. Testing

- **Unit (`tests/test_*.py`)** — mocked httpx via `respx`. No network. Cover: config loading, OAuth flow primitives, search-criteria building, refusal payloads, safety gates (confirm-amount, dollar cap, expired auctions).
- **Sandbox integration (`tests/test_live_sandbox.py`)** — gated on `EBAY_MCP_LIVE=1`. Hits `api.sandbox.ebay.com`. Tests search, watchlist add/remove, bid against a sandbox auction.
- **Production smoke (`tests/test_live_production.py`)** — gated on `EBAY_MCP_LIVE=1` **AND** `EBAY_MCP_LIVE_PRODUCTION=1`. Read-only by default. Hits real eBay. Never runs in CI.

## 10. Open questions

- **OAuth callback handling.** v0 uses manual code-paste flow. A local HTTP server on `localhost:8765` would be smoother but adds complexity. Punt.
- **Quantity > 1 buys.** `buy_now(quantity=2, confirm_amount=...)` — does confirm_amount mean per-unit or total? Total. Document explicitly.
- **Best-offer counter-offers.** If seller counters, do we expose accept/decline? Defer to Phase 2.
- **Time-zone handling for `ends_at`.** ISO 8601 with UTC offset. Caller responsible for local-time display.

## 11. Release plan

- **v0.0.x (Alpha):** skeleton + config + auth scaffold + `server_info` / `list_hosts`
- **v0.1.x (Alpha):** + client_credentials OAuth + `search` / `get_item` (Browse API)
- **v0.2.x (Alpha):** + user authorization-code flow + `get_watchlist` / `get_active_bids` / `get_won_items` / `get_lost_items` / `get_purchase_history`
- **v0.3.x (Beta):** + `add_to_watchlist` / `remove_from_watchlist`
- **v0.4.x (Beta):** + `place_bid` / `buy_now` / `make_best_offer` with full safety gates. Sandbox-only verification.
- **v0.5.x (Beta):** + production smoke, README polish, all CI workflows
- **v1.0.0:** trouble-free use against the live mailbox for ≥3 weeks; promote `Development Status` to `5 - Production/Stable`.

## 12. Considered + skipped

- **Playwright fallback for bidding.** Trading API still works for individual keys; no need for a fragile fallback.
- **Snipe-bidding scheduler.** Belongs in a higher-level orchestrator, not in this MCP.
- **Search-with-saved-query.** eBay's saved searches are a UI feature; not exposed in any clean API form. Skip.
