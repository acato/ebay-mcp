"""eBay API endpoint URLs.

These are deterministic per environment (sandbox vs production) and don't
belong in user config. Centralized here so a single import gives access
to whichever set the active host needs.
"""

from __future__ import annotations

SANDBOX_URLS = {
    "browse": "https://api.sandbox.ebay.com/buy/browse/v1",
    "trading": "https://api.sandbox.ebay.com/ws/api.dll",
    "oauth_token": "https://api.sandbox.ebay.com/identity/v1/oauth2/token",
    "oauth_authorize": "https://auth.sandbox.ebay.com/oauth2/authorize",
}

PRODUCTION_URLS = {
    "browse": "https://api.ebay.com/buy/browse/v1",
    "trading": "https://api.ebay.com/ws/api.dll",
    "oauth_token": "https://api.ebay.com/identity/v1/oauth2/token",
    "oauth_authorize": "https://auth.ebay.com/oauth2/authorize",
}


def urls_for_host(host: str) -> dict[str, str]:
    """Return the URL dict for a host name. Raises ValueError on unknown host."""
    if host == "sandbox":
        return SANDBOX_URLS
    if host == "production":
        return PRODUCTION_URLS
    raise ValueError(f"unknown host {host!r}: expected 'sandbox' or 'production'")
