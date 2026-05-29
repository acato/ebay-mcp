"""Tests for the config loader."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from ebay_mcp.config import Config, load_config


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(textwrap.dedent(body).strip() + "\n", encoding="utf-8")
    return p


def test_load_missing_returns_empty_hosts():
    cfg = load_config(Path("/no/such/file.toml"))
    assert isinstance(cfg, Config)
    assert cfg.hosts == {}
    assert cfg.default_host == "sandbox"


def test_load_minimal_sandbox(tmp_path):
    p = _write(
        tmp_path,
        """
        default_host = "sandbox"

        [hosts.sandbox]
        app_id = "AppId-AAA"
        dev_id = "DevId-BBB"
        cert_id = "CertId-CCC"
        """,
    )
    cfg = load_config(p)
    assert cfg.default_host == "sandbox"
    assert "sandbox" in cfg.hosts
    assert cfg.hosts["sandbox"].app_id == "AppId-AAA"
    assert cfg.hosts["sandbox"].redirect_uri == "https://localhost/oauth/callback"


def test_invalid_default_host_rejected(tmp_path):
    p = _write(
        tmp_path,
        """
        default_host = "staging"

        [hosts.sandbox]
        app_id = "x"
        dev_id = "y"
        cert_id = "z"
        """,
    )
    with pytest.raises(Exception, match="default_host"):
        load_config(p)


def test_invalid_host_name_rejected(tmp_path):
    p = _write(
        tmp_path,
        """
        [hosts.staging]
        app_id = "x"
        dev_id = "y"
        cert_id = "z"
        """,
    )
    with pytest.raises(Exception, match="unknown host"):
        load_config(p)


def test_resolve_cert_id_env_wins(tmp_path, monkeypatch):
    p = _write(
        tmp_path,
        """
        [hosts.sandbox]
        app_id = "x"
        dev_id = "y"
        cert_id = "from_file"
        """,
    )
    cfg = load_config(p)
    monkeypatch.setenv("EBAY_MCP_SANDBOX_CERT_ID", "from_env")
    assert cfg.resolve_cert_id("sandbox") == "from_env"


def test_resolve_cert_id_file_fallback(tmp_path, monkeypatch):
    p = _write(
        tmp_path,
        """
        [hosts.sandbox]
        app_id = "x"
        dev_id = "y"
        cert_id = "from_file"
        """,
    )
    cfg = load_config(p)
    monkeypatch.delenv("EBAY_MCP_SANDBOX_CERT_ID", raising=False)
    assert cfg.resolve_cert_id("sandbox") == "from_file"


def test_resolve_cert_id_missing(tmp_path, monkeypatch):
    p = _write(
        tmp_path,
        """
        [hosts.sandbox]
        app_id = "x"
        dev_id = "y"
        """,
    )
    cfg = load_config(p)
    monkeypatch.delenv("EBAY_MCP_SANDBOX_CERT_ID", raising=False)
    with pytest.raises(ValueError, match="no cert_id"):
        cfg.resolve_cert_id("sandbox")


def test_resolve_cert_id_unknown_host_raises(tmp_path):
    p = _write(
        tmp_path,
        """
        [hosts.sandbox]
        app_id = "x"
        dev_id = "y"
        cert_id = "z"
        """,
    )
    cfg = load_config(p)
    with pytest.raises(KeyError, match="production"):
        cfg.resolve_cert_id("production")


def test_resolve_host_uses_default_when_none(tmp_path):
    p = _write(
        tmp_path,
        """
        default_host = "sandbox"

        [hosts.sandbox]
        app_id = "x"
        dev_id = "y"
        cert_id = "z"
        """,
    )
    cfg = load_config(p)
    assert cfg.resolve_host(None) == "sandbox"


def test_resolve_host_explicit_param(tmp_path):
    p = _write(
        tmp_path,
        """
        default_host = "sandbox"

        [hosts.sandbox]
        app_id = "x"
        dev_id = "y"
        cert_id = "z"

        [hosts.production]
        app_id = "p"
        dev_id = "q"
        cert_id = "r"
        """,
    )
    cfg = load_config(p)
    assert cfg.resolve_host("production") == "production"


def test_resolve_host_unconfigured_raises(tmp_path):
    p = _write(
        tmp_path,
        """
        [hosts.sandbox]
        app_id = "x"
        dev_id = "y"
        cert_id = "z"
        """,
    )
    cfg = load_config(p)
    with pytest.raises(KeyError, match="production"):
        cfg.resolve_host("production")
