"""MCP server entry point for ebay-mcp.

Day 1 skeleton: registers the FastMCP server and two diagnostic tools
(`server_info`, `list_hosts`) that exercise the config loader. eBay API
tools (search, watchlist, bidding, etc.) land in Day 1b onward.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from ebay_mcp import __version__
from ebay_mcp.config import Config, config_path, load_config

mcp = FastMCP("ebay-mcp")


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


def main() -> None:
    """Console-script entry point. Runs the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
