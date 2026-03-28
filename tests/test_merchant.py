"""Tests for shop merchant commands.

Tests SSRF guard, UCP Business Profile discovery (/.well-known/ucp),
merchants.yaml persistence, CLI wiring, and Shopify Catalog connect.
"""

from __future__ import annotations

import json
import socket
from unittest.mock import patch

import httpx
import pytest
import respx
import yaml
from typer.testing import CliRunner

from shop.cli import app
from shop.commands.merchant import run_merchant_add_command, run_merchant_connect_shopify_command

runner = CliRunner()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PUBLIC_ADDR = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))]


def _mock_public_dns(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *args, **kwargs: _PUBLIC_ADDR,
    )


def _ucp_profile(endpoint: str, name: str | None = None) -> dict:
    """Build a minimal UCP Business Profile JSON response."""
    profile: dict = {
        "ucp": {
            "version": "2026-01-23",
            "services": [
                {
                    "id": "dev.ucp.shopping",
                    "transports": [{"type": "rest", "endpoint": endpoint}],
                    "capabilities": ["dev.ucp.shopping.checkout"],
                }
            ],
        }
    }
    if name:
        profile["name"] = name
    return profile


# ---------------------------------------------------------------------------
# SSRF guard
# ---------------------------------------------------------------------------


class TestSSRFGuard:
    def test_http_url_exits_1(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as exc_info:
            run_merchant_add_command("http://example.com", tmp_path / "merchants.yaml")
        assert exc_info.value.code == 1
        data = json.loads(capsys.readouterr().out)
        assert data["error_code"] == "invalid_url"
        assert "HTTPS" in data["detail"]

    def test_ftp_url_exits_1(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as exc_info:
            run_merchant_add_command("ftp://example.com", tmp_path / "merchants.yaml")
        assert exc_info.value.code == 1
        data = json.loads(capsys.readouterr().out)
        assert data["error_code"] == "invalid_url"

    def test_private_ip_192_168_exits_1(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as exc_info:
            run_merchant_add_command("https://192.168.1.100", tmp_path / "merchants.yaml")
        assert exc_info.value.code == 1
        data = json.loads(capsys.readouterr().out)
        assert data["error_code"] == "invalid_url"

    def test_private_ip_10_x_exits_1(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as exc_info:
            run_merchant_add_command("https://10.0.0.1", tmp_path / "merchants.yaml")
        assert exc_info.value.code == 1

    def test_private_ip_172_16_exits_1(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as exc_info:
            run_merchant_add_command("https://172.16.0.1", tmp_path / "merchants.yaml")
        assert exc_info.value.code == 1

    def test_loopback_127_exits_1(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as exc_info:
            run_merchant_add_command("https://127.0.0.1", tmp_path / "merchants.yaml")
        assert exc_info.value.code == 1

    def test_loopback_localhost_exits_1(self, tmp_path, capsys):
        """localhost resolves to 127.0.0.1 — must be blocked."""
        with pytest.raises(SystemExit) as exc_info:
            run_merchant_add_command("https://localhost", tmp_path / "merchants.yaml")
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# UCP Business Profile discovery
# ---------------------------------------------------------------------------


class TestDiscovery:
    def test_ucp_profile_found(self, monkeypatch, tmp_path, capsys):
        _mock_public_dns(monkeypatch)
        with respx.mock:
            respx.get("https://example.com/.well-known/ucp").mock(
                return_value=httpx.Response(
                    200, json=_ucp_profile("https://api.example.com/ucp", "Example Store")
                )
            )

            with pytest.raises(SystemExit) as exc_info:
                run_merchant_add_command("https://example.com", tmp_path / "merchants.yaml")

        assert exc_info.value.code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["status"] == "added"
        assert data["merchant"]["ucp_endpoint"] == "https://api.example.com/ucp"
        assert data["merchant"]["name"] == "Example Store"

    def test_ucp_profile_not_found_exits_4(self, monkeypatch, tmp_path, capsys):
        _mock_public_dns(monkeypatch)
        with respx.mock:
            respx.get("https://example.com/.well-known/ucp").mock(return_value=httpx.Response(404))

            with pytest.raises(SystemExit) as exc_info:
                run_merchant_add_command("https://example.com", tmp_path / "merchants.yaml")

        assert exc_info.value.code == 4
        data = json.loads(capsys.readouterr().out)
        assert data["error_code"] == "merchant_not_discoverable"

    def test_ucp_profile_missing_endpoint_exits_4(self, monkeypatch, tmp_path, capsys):
        """Profile exists but has no services/transports → not discoverable."""
        _mock_public_dns(monkeypatch)
        with respx.mock:
            respx.get("https://example.com/.well-known/ucp").mock(
                return_value=httpx.Response(
                    200, json={"ucp": {"version": "2026-01-23", "services": []}}
                )
            )

            with pytest.raises(SystemExit) as exc_info:
                run_merchant_add_command("https://example.com", tmp_path / "merchants.yaml")

        assert exc_info.value.code == 4
        data = json.loads(capsys.readouterr().out)
        assert data["error_code"] == "merchant_not_discoverable"

    def test_slug_derived_from_hostname(self, monkeypatch, tmp_path, capsys):
        _mock_public_dns(monkeypatch)
        with respx.mock:
            respx.get("https://shop.acme.com/.well-known/ucp").mock(
                return_value=httpx.Response(200, json=_ucp_profile("https://api.acme.com/ucp"))
            )

            with pytest.raises(SystemExit):
                run_merchant_add_command("https://shop.acme.com", tmp_path / "merchants.yaml")

        data = json.loads(capsys.readouterr().out)
        assert data["merchant"]["slug"] == "shop-acme-com"

    def test_www_stripped_from_slug(self, monkeypatch, tmp_path, capsys):
        _mock_public_dns(monkeypatch)
        with respx.mock:
            respx.get("https://www.example.com/.well-known/ucp").mock(
                return_value=httpx.Response(200, json=_ucp_profile("https://api.example.com/ucp"))
            )

            with pytest.raises(SystemExit):
                run_merchant_add_command("https://www.example.com", tmp_path / "merchants.yaml")

        data = json.loads(capsys.readouterr().out)
        assert data["merchant"]["slug"] == "example-com"

    def test_name_derived_from_slug_when_absent(self, monkeypatch, tmp_path, capsys):
        """Profile with no name field → name derived from slug."""
        _mock_public_dns(monkeypatch)
        with respx.mock:
            respx.get("https://example.com/.well-known/ucp").mock(
                return_value=httpx.Response(
                    200,
                    json=_ucp_profile("https://api.example.com/ucp"),  # no name
                )
            )

            with pytest.raises(SystemExit):
                run_merchant_add_command("https://example.com", tmp_path / "merchants.yaml")

        data = json.loads(capsys.readouterr().out)
        assert data["merchant"]["name"] == "Example Com"


# ---------------------------------------------------------------------------
# merchants.yaml persistence
# ---------------------------------------------------------------------------


class TestMerchantsYamlPersistence:
    def test_creates_merchants_yaml(self, monkeypatch, tmp_path, capsys):
        merchants_path = tmp_path / "merchants.yaml"
        assert not merchants_path.exists()

        _mock_public_dns(monkeypatch)
        with respx.mock:
            respx.get("https://example.com/.well-known/ucp").mock(
                return_value=httpx.Response(200, json=_ucp_profile("https://api.example.com/ucp"))
            )
            with pytest.raises(SystemExit):
                run_merchant_add_command("https://example.com", merchants_path)

        assert merchants_path.exists()

    def test_merchant_written_to_yaml(self, monkeypatch, tmp_path, capsys):
        merchants_path = tmp_path / "merchants.yaml"

        _mock_public_dns(monkeypatch)
        with respx.mock:
            respx.get("https://example.com/.well-known/ucp").mock(
                return_value=httpx.Response(
                    200, json=_ucp_profile("https://api.example.com/ucp", "Example Store")
                )
            )
            with pytest.raises(SystemExit):
                run_merchant_add_command("https://example.com", merchants_path)

        with merchants_path.open() as f:
            saved = yaml.safe_load(f)

        assert len(saved["merchants"]) == 1
        m = saved["merchants"][0]
        assert m["slug"] == "example-com"
        assert m["adapter"] == "ucp"
        assert m["ucp_endpoint"] == "https://api.example.com/ucp"
        assert m["name"] == "Example Store"

    def test_appends_to_existing_merchants(self, monkeypatch, tmp_path, capsys):
        merchants_path = tmp_path / "merchants.yaml"
        with merchants_path.open("w") as f:
            yaml.dump(
                {"merchants": [{"slug": "existing", "name": "Existing", "adapter": "mock"}]}, f
            )

        _mock_public_dns(monkeypatch)
        with respx.mock:
            respx.get("https://example.com/.well-known/ucp").mock(
                return_value=httpx.Response(200, json=_ucp_profile("https://api.example.com/ucp"))
            )
            with pytest.raises(SystemExit):
                run_merchant_add_command("https://example.com", merchants_path)

        with merchants_path.open() as f:
            saved = yaml.safe_load(f)

        slugs = [m["slug"] for m in saved["merchants"]]
        assert "existing" in slugs
        assert "example-com" in slugs

    def test_duplicate_slug_is_upserted(self, monkeypatch, tmp_path, capsys):
        merchants_path = tmp_path / "merchants.yaml"
        with merchants_path.open("w") as f:
            yaml.dump(
                {"merchants": [{"slug": "example-com", "name": "Old Name", "adapter": "ucp"}]}, f
            )

        _mock_public_dns(monkeypatch)
        with respx.mock:
            respx.get("https://example.com/.well-known/ucp").mock(
                return_value=httpx.Response(
                    200, json=_ucp_profile("https://api.example.com/ucp", "New Name")
                )
            )
            with pytest.raises(SystemExit):
                run_merchant_add_command("https://example.com", merchants_path)

        with merchants_path.open() as f:
            saved = yaml.safe_load(f)

        assert len(saved["merchants"]) == 1
        assert saved["merchants"][0]["name"] == "New Name"


# ---------------------------------------------------------------------------
# Shopify Catalog connect
# ---------------------------------------------------------------------------


class TestConnectShopify:
    def test_connect_shopify_success(self, tmp_path, capsys):
        merchants_path = tmp_path / "merchants.yaml"
        with respx.mock:
            respx.post("https://api.shopify.com/auth/access_token").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "access_token": "shpat_test",
                        "scope": "read_global_api_catalog_search",
                        "expires_in": 3600,
                    },
                )
            )

            with pytest.raises(SystemExit) as exc_info:
                run_merchant_connect_shopify_command(
                    "test_client_id", "test_secret", "US", merchants_path
                )

        assert exc_info.value.code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["status"] == "connected"
        assert data["merchant"]["adapter"] == "shopify_catalog"
        assert data["merchant"]["ships_to"] == "US"

    def test_connect_shopify_saves_credentials(self, tmp_path, capsys):
        merchants_path = tmp_path / "merchants.yaml"
        with respx.mock:
            respx.post("https://api.shopify.com/auth/access_token").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "access_token": "shpat_test",
                        "scope": "read_global_api_catalog_search",
                        "expires_in": 3600,
                    },
                )
            )
            with pytest.raises(SystemExit):
                run_merchant_connect_shopify_command("cid", "csec", "GB", merchants_path)

        with merchants_path.open() as f:
            saved = yaml.safe_load(f)

        m = saved["merchants"][0]
        assert m["slug"] == "shopify"
        assert m["adapter"] == "shopify_catalog"
        assert m["client_id"] == "cid"
        assert m["ships_to"] == "GB"

    def test_connect_shopify_invalid_creds_exits_2(self, tmp_path, capsys):
        with respx.mock:
            respx.post("https://api.shopify.com/auth/access_token").mock(
                return_value=httpx.Response(401)
            )

            with pytest.raises(SystemExit) as exc_info:
                run_merchant_connect_shopify_command(
                    "bad_id", "bad_secret", "US", tmp_path / "merchants.yaml"
                )

        assert exc_info.value.code == 2
        data = json.loads(capsys.readouterr().out)
        assert data["error_code"] == "auth_failed"


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


class TestCLIWiring:
    def test_missing_url_exits_nonzero(self):
        result = runner.invoke(app, ["merchant", "add"])
        assert result.exit_code != 0

    def test_http_url_via_cli_exits_1(self):
        result = runner.invoke(app, ["merchant", "add", "http://example.com"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["error_code"] == "invalid_url"

    def test_success_via_cli(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda host, port, *args, **kwargs: _PUBLIC_ADDR,
        )
        with (
            patch("shop.commands.merchant.MERCHANTS_PATH", tmp_path / "merchants.yaml"),
            respx.mock,
        ):
            respx.get("https://example.com/.well-known/ucp").mock(
                return_value=httpx.Response(200, json=_ucp_profile("https://api.example.com/ucp"))
            )

            result = runner.invoke(app, ["merchant", "add", "https://example.com"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "added"

    def test_connect_shopify_via_cli(self, tmp_path):
        with (
            patch("shop.commands.merchant.MERCHANTS_PATH", tmp_path / "merchants.yaml"),
            respx.mock,
        ):
            respx.post("https://api.shopify.com/auth/access_token").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "access_token": "tok",
                        "scope": "read_global_api_catalog_search",
                        "expires_in": 3600,
                    },
                )
            )

            result = runner.invoke(
                app,
                [
                    "merchant",
                    "connect-shopify",
                    "--client-id",
                    "cid",
                    "--client-secret",
                    "csec",
                ],
            )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "connected"


# ---------------------------------------------------------------------------
# ACP merchant discovery
# ---------------------------------------------------------------------------


def _acp_profile(endpoint: str, name: str | None = None) -> dict:
    profile: dict = {
        "version": "1.0",
        "acp": {
            "endpoint": endpoint,
            "payment_handlers": ["stripe"],
            "currency": "USD",
        },
    }
    if name:
        profile["name"] = name
    return profile


class TestMerchantAddACP:
    def test_discovers_and_saves_acp_merchant(self, monkeypatch, tmp_path, capsys):
        from shop.commands.merchant import run_merchant_add_acp_command

        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: _PUBLIC_ADDR)
        merchants_path = tmp_path / "merchants.yaml"

        with respx.mock:
            respx.get("https://acp.example.com/.well-known/acp").mock(
                return_value=httpx.Response(
                    200, json=_acp_profile("https://acp.example.com/api/acp", "ACP Store")
                )
            )
            with pytest.raises(SystemExit) as exc_info:
                run_merchant_add_acp_command(
                    url="https://acp.example.com",
                    acp_key="acp_key_test",
                    merchants_path=merchants_path,
                )

        assert exc_info.value.code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["status"] == "added"
        assert data["merchant"]["adapter"] == "acp"
        assert data["merchant"]["acp_endpoint"] == "https://acp.example.com/api/acp"
        assert data["merchant"]["name"] == "ACP Store"

    def test_saves_acp_key_in_merchant_config(self, monkeypatch, tmp_path, capsys):
        from shop.commands.merchant import run_merchant_add_acp_command

        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: _PUBLIC_ADDR)
        merchants_path = tmp_path / "merchants.yaml"

        with respx.mock:
            respx.get("https://acp.example.com/.well-known/acp").mock(
                return_value=httpx.Response(
                    200, json=_acp_profile("https://acp.example.com/api/acp")
                )
            )
            with pytest.raises(SystemExit):
                run_merchant_add_acp_command(
                    url="https://acp.example.com",
                    acp_key="my-secret-key",
                    merchants_path=merchants_path,
                )

        saved = yaml.safe_load(merchants_path.read_text())
        merchant = saved["merchants"][0]
        assert merchant["acp_key"] == "my-secret-key"

    def test_no_well_known_acp_exits_4(self, monkeypatch, tmp_path, capsys):
        from shop.commands.merchant import run_merchant_add_acp_command

        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: _PUBLIC_ADDR)

        with respx.mock:
            respx.get("https://acp.example.com/.well-known/acp").mock(
                return_value=httpx.Response(404)
            )
            with pytest.raises(SystemExit) as exc_info:
                run_merchant_add_acp_command(
                    url="https://acp.example.com",
                    merchants_path=tmp_path / "merchants.yaml",
                )

        assert exc_info.value.code == 4
        data = json.loads(capsys.readouterr().out)
        assert data["error_code"] == "merchant_not_discoverable"

    def test_ssrf_guard_applies_to_acp(self, tmp_path, capsys):
        from shop.commands.merchant import run_merchant_add_acp_command

        with pytest.raises(SystemExit) as exc_info:
            run_merchant_add_acp_command(
                url="http://acp.example.com",  # HTTP not HTTPS
                merchants_path=tmp_path / "merchants.yaml",
            )
        assert exc_info.value.code == 1
        data = json.loads(capsys.readouterr().out)
        assert data["error_code"] == "invalid_url"
