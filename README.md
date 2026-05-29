# ebay-mcp

MCP server for eBay buyer-side workflows: search, watch, bid, buy, manage MyeBay. Hybrid stack — modern Buy/Browse REST API for search and item lookup, legacy Trading API (still functional in 2026) for everything stateful.

> ⚠️ **Beta (v0.4.0).** Browse + Trading APIs wired, sandbox + production both supported. Money-commit tools live behind safety gates — read the [money commits](#money-commits-high-risk--safety-gated) section before enabling production.

## Scope

### Read-only (low risk)
- `search`, `get_item` (Browse API)
- `get_watchlist`, `get_active_bids`, `get_won_items`, `get_lost_items`, `get_purchase_history` (Trading API)

### Watchlist writes (low risk — no money commit)
- `add_to_watchlist`, `remove_from_watchlist`

### Money commits (high risk — safety-gated)

- `place_bid(item_id, max_bid_amount, confirm_amount, ...)` — Trading `PlaceOffer` with `Action=Bid`. Proxy bid on an auction.
- `buy_now(item_id, confirm_amount, ...)` — Trading `PlaceOffer` with `Action=Purchase`. Commits to a Buy-It-Now sale.
- `make_best_offer(item_id, offer_amount, confirm_amount, ...)` — Trading `PlaceOffer` with `Action=BestOffer`. Submits an offer the seller can accept/counter/decline.

**Safety stack (every money tool):**

1. **Confirm-amount gate.** Every call requires `confirm_amount` exactly equal to the bid/buy/offer amount. The LLM has to repeat the dollar figure; mismatches return a structured refusal payload (not an exception) so the LLM can read it and retry with corrected params.
2. **$500 per-call cap.** Amounts above $500 refuse with `reason: "cap_exceeded"`. Two ways to authorize a higher spend:
   - **Per-call:** pass `max_bid_override >= amount` on the tool call. Lower overrides refuse with `reason: "override_too_low"`.
   - **Operator-wide:** set `EBAY_MCP_ALLOW_HIGH_VALUE=1` in the MCP server's environment.
3. **Active-host visibility.** Successful calls against `default_host = "production"` include a `warning` field in the response (`"PRODUCTION HOST — this call committed real money on eBay."`). `server_info()` flags the active host before any call.
4. **No silent half-commits.** No auth token cached → the underlying Trading API call raises before touching eBay, not mid-flight.

All money tools also surface eBay-side errors (insufficient bid, currency mismatch, listing ended, etc.) as `TradingApiError` with the parsed `Errors` block attached.

### Out of scope (v0)
- ❌ Selling side (use eBay's Sell API directly for that — different shape entirely)
- ❌ Snipe-bidding with auto-trigger (one tool call = one bid, no scheduling)
- ❌ Multi-account: v0 is one user account per host config; add multi-account later if needed

## Install

> Not yet on PyPI. From source:

```bash
git clone https://github.com/acato/ebay-mcp
cd ebay-mcp
uv sync
uv run ebay-mcp
```

### Windows: avoid Microsoft Store Python

If `uv` picks Microsoft Store Python (path under `\WindowsApps\PythonSoftwareFoundation...`) when creating the venv, the MCP runs fine from a terminal but fails to launch from GUI hosts like the Claude desktop app, IDE extensions, or scheduled tasks. You will see:

```
Unable to create process using "...\WindowsApps\PythonSoftwareFoundation.Python.3.12_...\python.exe"
```

Pin `uv` to a non-Store interpreter — uv's managed Python is easiest:

```powershell
uv python install 3.12
uv venv --python 3.12 --python-preference only-managed --clear
uv sync
```

Verify: `Get-Content .venv\pyvenv.cfg` — the `home =` line should point under `AppData\Roaming\uv\python\...`, **not** `\WindowsApps\`.

## Configure

Sandbox-first. Get your sandbox keyset from [developer.ebay.com](https://developer.ebay.com) → Application Keysets → Sandbox. Copy `examples/config.toml` to `~/.config/ebay-mcp/config.toml`:

```toml
default_host = "sandbox"

[hosts.sandbox]
app_id = "..."             # SBX App ID (Client ID)
dev_id = "..."             # Developer Account Dev ID
cert_id = "..."            # SBX Cert ID (Client Secret) — env var preferred
redirect_uri = "https://localhost/oauth/callback"

# [hosts.production]
# app_id = "..."
# dev_id = "..."
# cert_id = "..."
# redirect_uri = "https://localhost/oauth/callback"
```

Per-host env vars override file values:
- `EBAY_MCP_<HOST>_CERT_ID` — overrides `cert_id` (recommended for production)
- `EBAY_MCP_<HOST>_APP_ID`, `EBAY_MCP_<HOST>_DEV_ID` — overrides for completeness

User-level OAuth tokens cache to `~/.config/ebay-mcp/token-cache-<host>.json` automatically after the manual authorization-code flow (see `auth.py` docs once Day 1b lands).

## Use with Claude Code

```bash
claude mcp add ebay-mcp -- uv run --directory /path/to/ebay-mcp ebay-mcp
```

## Use with Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "ebay-mcp": {
      "command": "C:\\Users\\you\\AppData\\Local\\Microsoft\\WinGet\\Links\\uv.exe",
      "args": ["run", "--directory", "C:\\path\\to\\ebay-mcp", "ebay-mcp"]
    }
  }
}
```

## Documentation

- [DESIGN.md](DESIGN.md) — architecture, tool surface, safety patterns, OAuth flow
- [CONTRIBUTING.md](CONTRIBUTING.md) — dev setup, sandbox testing, release process

## License

Apache 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

## Trademarks

"eBay" is a trademark of eBay Inc. This project is an independent integration with the standard eBay public APIs and is not affiliated with, endorsed by, or sponsored by eBay Inc.
