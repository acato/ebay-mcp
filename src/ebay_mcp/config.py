"""Configuration loading for ebay-mcp.

Reads ~/.config/ebay-mcp/config.toml (or EBAY_MCP_CONFIG override), merges
environment-variable credentials, validates with pydantic.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "ebay-mcp" / "config.toml"
ENV_CONFIG_PATH = "EBAY_MCP_CONFIG"

VALID_HOST_NAMES = {"sandbox", "production"}


class Host(BaseModel):
    """Per-environment credentials. Sandbox and production differ only in keys."""

    app_id: str
    dev_id: str
    cert_id: str | None = None  # may be sourced from env var
    redirect_uri: str = "https://localhost/oauth/callback"


class Config(BaseModel):
    default_host: str = "sandbox"
    hosts: dict[str, Host] = Field(default_factory=dict)

    @field_validator("default_host")
    @classmethod
    def _default_host_valid_name(cls, v: str) -> str:
        if v not in VALID_HOST_NAMES:
            raise ValueError(f"default_host must be one of {sorted(VALID_HOST_NAMES)}, got {v!r}")
        return v

    @field_validator("hosts")
    @classmethod
    def _host_names_valid(cls, v: dict[str, Host]) -> dict[str, Host]:
        for name in v:
            if name not in VALID_HOST_NAMES:
                raise ValueError(
                    f"unknown host {name!r}: expected one of {sorted(VALID_HOST_NAMES)}"
                )
        return v

    def resolve_cert_id(self, host_name: str) -> str:
        """Return the Cert ID (Client Secret) for a host, env-var first then file.

        Raises:
            KeyError: if the host isn't configured at all.
            ValueError: if no Cert ID is available from any source.
        """
        if host_name not in self.hosts:
            raise KeyError(
                f"unknown host {host_name!r}: not in config.hosts (got {sorted(self.hosts)})"
            )
        env_var = f"EBAY_MCP_{host_name.upper()}_CERT_ID"
        if os.environ.get(env_var):
            return os.environ[env_var]
        cert = self.hosts[host_name].cert_id
        if cert:
            return cert
        raise ValueError(
            f"no cert_id for host {host_name!r}: "
            f"set {env_var} or hosts.{host_name}.cert_id in config"
        )

    def resolve_host(self, requested: str | None) -> str:
        """Return the host name to use: explicit param wins, else default_host.

        Validates the result is a configured host.
        """
        name = requested or self.default_host
        if name not in self.hosts:
            raise KeyError(f"host {name!r} not configured (configured: {sorted(self.hosts)})")
        return name


def config_path() -> Path:
    """Return the active config path (env-var override or default)."""
    override = os.environ.get(ENV_CONFIG_PATH)
    return Path(override).expanduser() if override else DEFAULT_CONFIG_PATH


def load_config(path: Path | None = None) -> Config:
    """Load and validate config from disk. Returns empty-hosts Config if file missing."""
    target = path or config_path()
    if not target.exists():
        return Config()
    with target.open("rb") as fh:
        raw = tomllib.load(fh)
    return Config(**raw)
