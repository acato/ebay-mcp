"""Smoke tests: package imports + MCP server boots + URL constants are right."""

from __future__ import annotations


def test_package_imports():
    import ebay_mcp

    assert hasattr(ebay_mcp, "__version__")


def test_server_module_imports():
    from ebay_mcp import server

    assert hasattr(server, "main")
    assert hasattr(server, "mcp")


def test_urls_module_distinguishes_environments():
    from ebay_mcp.urls import PRODUCTION_URLS, SANDBOX_URLS, urls_for_host

    # Sandbox URLs must contain "sandbox"; production must not.
    for key, url in SANDBOX_URLS.items():
        assert "sandbox" in url, f"sandbox URL for {key} missing 'sandbox': {url}"
    for key, url in PRODUCTION_URLS.items():
        assert "sandbox" not in url, f"production URL for {key} leaks 'sandbox': {url}"

    assert urls_for_host("sandbox") == SANDBOX_URLS
    assert urls_for_host("production") == PRODUCTION_URLS


def test_urls_for_host_rejects_unknown():
    import pytest

    from ebay_mcp.urls import urls_for_host

    with pytest.raises(ValueError, match="unknown host"):
        urls_for_host("staging")


def test_server_info_runs_without_config(tmp_path, monkeypatch):
    monkeypatch.setenv("EBAY_MCP_CONFIG", str(tmp_path / "nope.toml"))
    from ebay_mcp.server import server_info

    info = server_info()
    assert "version" in info
    assert info["config_exists"] == "False"
    assert info["active_host"] == "sandbox"
    assert info["configured_hosts"] == []
    # No production warning when sandbox is default.
    assert "warning" not in info


def test_server_info_warns_on_production_default(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text(
        'default_host = "production"\n\n'
        "[hosts.production]\n"
        'app_id = "x"\ndev_id = "y"\ncert_id = "z"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("EBAY_MCP_CONFIG", str(p))
    from ebay_mcp.server import server_info

    info = server_info()
    assert info["active_host"] == "production"
    assert "warning" in info
    assert "PRODUCTION" in info["warning"]


def test_list_hosts_reports_credential_status(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text(
        '[hosts.sandbox]\napp_id = "AppId"\ndev_id = "DevId"\ncert_id = "CertId"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("EBAY_MCP_CONFIG", str(p))
    monkeypatch.delenv("EBAY_MCP_SANDBOX_CERT_ID", raising=False)
    from ebay_mcp.server import list_hosts

    result = list_hosts()
    assert len(result) == 1
    s = result[0]
    assert s["name"] == "sandbox"
    assert s["is_default"] is True
    assert s["app_id_set"] is True
    assert s["cert_id_in_file"] is True
    assert s["cert_id_in_env"] is False
    assert s["credentials_ready"] is True
