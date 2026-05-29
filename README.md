# ebay-mcp

MCP server for eBay buyer-side workflows: search, watch, bid, buy, manage MyeBay. Hybrid stack — modern Buy/Browse REST API for search and item lookup, legacy Trading API (still functional in 2026) for everything stateful.

> ⚠️ **Alpha.** Day 1 skeleton. Sandbox-first development; production not yet wired.

## Scope

### Read-only (low risk)
- `search`, `get_item` (Browse API)
- `get_watchlist`, `get_active_bids`, `get_won_items`, `get_lost_items`, `get_purchase_history` (Trading API)

### Watchlist writes (low risk — no money commit)
- `add_to_watchlist`, `remove_from_watchlist`

### Money commits (high risk — safety-gated)
- `place_bid`, `buy_now`, `make_best_offer` (Trading API `PlaceOffer`)
- Each requires `confirm_amount` exactly equal to the proposed value.
- Per-call cap: **$500**. Higher refused without `EBAY_MCP_ALLOW_HIGH_VALUE=1` or per-call `max_bid_override`.
- Refusals are structured payloads, not exceptions — the LLM gets enough info to retry with corrected params.

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
