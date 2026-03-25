"""Tests for shop merchant add command.

Tests SSRF guard, UCP endpoint discovery, health check,
merchants.yaml persistence, and CLI wiring.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx
import yaml
from typer.testing import CliRunner

from shop.cli import app
from shop.commands.merchant import run_merchant_add_command

runner = CliRunner()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# A fake public IP returned by monkeypatched getaddrinfo
_PUBLIC_ADDR = [
    (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))
]


def _mock_public_dns(monkeypatch):
    """Patch getaddrinfo to return a public IP — bypasses SSRF block."""
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, port, *args, **kwargs: _PUBLIC_ADDR,
    )


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
        # Don't monkeypatch — let real DNS resolve localhost to 127.0.0.1
        with pytest.raises(SystemExit) as exc_info:
            run_merchant_add_command("https://localhost", tmp_path / "merchants.yaml")
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class TestDiscovery:
    def test_commerce_txt_found(self, monkeypatch, tmp_path, capsys):
        _mock_public_dns(monkeypatch)
        with respx.mock:
            respx.get("https://example.com/.well-known/commerce.txt").mock(
                return_value=httpx.Response(
                    200, text="UCP-Endpoint: https://api.example.com/ucp\nName: Example Store\n"
                )
            )
            respx.get("https://api.example.com/ucp/capabilities").mock(
                return_value=httpx.Response(200, json={"search": True, "order_create": True})
            )

            with pytest.raises(SystemExit) as exc_info:
                run_merchant_add_command("https://example.com", tmp_path / "merchants.yaml")

        assert exc_info.value.code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["status"] == "added"
        assert data["merchant"]["ucp_endpoint"] == "https://api.example.com/ucp"
        assert data["merchant"]["name"] == "Example Store"

    def test_ucp_json_fallback(self, monkeypatch, tmp_path, capsys):
        """commerce.txt returns 404 → falls back to ucp.json."""
        _mock_public_dns(monkeypatch)
        with respx.mock:
            respx.get("https://example.com/.well-known/commerce.txt").mock(
                return_value=httpx.Response(404)
            )
            respx.get("https://example.com/.well-known/ucp.json").mock(
                return_value=httpx.Response(
                    200,
                    json={"ucp_endpoint": "https://api.example.com/ucp/v2", "name": "Example v2"},
                )
            )
            respx.get("https://api.example.com/ucp/v2/capabilities").mock(
                return_value=httpx.Response(200, json={"search": True})
            )

            with pytest.raises(SystemExit) as exc_info:
                run_merchant_add_command("https://example.com", tmp_path / "merchants.yaml")

        assert exc_info.value.code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["merchant"]["ucp_endpoint"] == "https://api.example.com/ucp/v2"

    def test_both_not_found_exits_4(self, monkeypatch, tmp_path, capsys):
        _mock_public_dns(monkeypatch)
        with respx.mock:
            respx.get("https://example.com/.well-known/commerce.txt").mock(
                return_value=httpx.Response(404)
            )
            respx.get("https://example.com/.well-known/ucp.json").mock(
                return_value=httpx.Response(404)
            )

            with pytest.raises(SystemExit) as exc_info:
                run_merchant_add_command("https://example.com", tmp_path / "merchants.yaml")

        assert exc_info.value.code == 4
        data = json.loads(capsys.readouterr().out)
        assert data["error_code"] == "merchant_not_discoverable"

    def test_commerce_txt_missing_endpoint_falls_back(self, monkeypatch, tmp_path, capsys):
        """commerce.txt exists but has no UCP-Endpoint line → try ucp.json."""
        _mock_public_dns(monkeypatch)
        with respx.mock:
            respx.get("https://example.com/.well-known/commerce.txt").mock(
                return_value=httpx.Response(200, text="Name: Example Store\n")
            )
            respx.get("https://example.com/.well-known/ucp.json").mock(
                return_value=httpx.Response(
                    200, json={"ucp_endpoint": "https://api.example.com/v1"}
                )
            )
            respx.get("https://api.example.com/v1/capabilities").mock(
                return_value=httpx.Response(200, json={})
            )

            with pytest.raises(SystemExit) as exc_info:
                run_merchant_add_command("https://example.com", tmp_path / "merchants.yaml")

        assert exc_info.value.code == 0

    def test_slug_derived_from_hostname(self, monkeypatch, tmp_path, capsys):
        _mock_public_dns(monkeypatch)
        with respx.mock:
            respx.get("https://shop.acme.com/.well-known/commerce.txt").mock(
                return_value=httpx.Response(
                    200, text="UCP-Endpoint: https://api.acme.com/ucp\n"
                )
            )
            respx.get("https://api.acme.com/ucp/capabilities").mock(
                return_value=httpx.Response(200, json={})
            )

            with pytest.raises(SystemExit):
                run_merchant_add_command("https://shop.acme.com", tmp_path / "merchants.yaml")

        data = json.loads(capsys.readouterr().out)
        assert data["merchant"]["slug"] == "shop-acme-com"

    def test_www_stripped_from_slug(self, monkeypatch, tmp_path, capsys):
        _mock_public_dns(monkeypatch)
        with respx.mock:
            respx.get("https://www.example.com/.well-known/commerce.txt").mock(
                return_value=httpx.Response(
                    200, text="UCP-Endpoint: https://api.example.com/ucp\n"
                )
            )
            respx.get("https://api.example.com/ucp/capabilities").mock(
                return_value=httpx.Response(200, json={})
            )

            with pytest.raises(SystemExit):
                run_merchant_add_command("https://www.example.com", tmp_path / "merchants.yaml")

        data = json.loads(capsys.readouterr().out)
        assert data["merchant"]["slug"] == "example-com"


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


class TestHealthCheck:
    def test_health_check_pass_no_warning(self, monkeypatch, tmp_path, capsys):
        _mock_public_dns(monkeypatch)
        with respx.mock:
            respx.get("https://example.com/.well-known/commerce.txt").mock(
                return_value=httpx.Response(200, text="UCP-Endpoint: https://api.example.com/ucp\n")
            )
            respx.get("https://api.example.com/ucp/capabilities").mock(
                return_value=httpx.Response(200, json={"search": True})
            )

            with pytest.raises(SystemExit):
                run_merchant_add_command("https://example.com", tmp_path / "merchants.yaml")

        data = json.loads(capsys.readouterr().out)
        assert "warning" not in data

    def test_health_check_500_adds_warning(self, monkeypatch, tmp_path, capsys):
        _mock_public_dns(monkeypatch)
        with respx.mock:
            respx.get("https://example.com/.well-known/commerce.txt").mock(
                return_value=httpx.Response(200, text="UCP-Endpoint: https://api.example.com/ucp\n")
            )
            respx.get("https://api.example.com/ucp/capabilities").mock(
                return_value=httpx.Response(500)
            )

            with pytest.raises(SystemExit) as exc_info:
                run_merchant_add_command("https://example.com", tmp_path / "merchants.yaml")

        assert exc_info.value.code == 0  # still added despite warning
        data = json.loads(capsys.readouterr().out)
        assert "warning" in data
        assert "health check failed" in data["warning"]

    def test_health_check_timeout_adds_warning(self, monkeypatch, tmp_path, capsys):
        _mock_public_dns(monkeypatch)
        with respx.mock:
            respx.get("https://example.com/.well-known/commerce.txt").mock(
                return_value=httpx.Response(200, text="UCP-Endpoint: https://api.example.com/ucp\n")
            )
            respx.get("https://api.example.com/ucp/capabilities").mock(
                side_effect=httpx.TimeoutException("timeout")
            )

            with pytest.raises(SystemExit) as exc_info:
                run_merchant_add_command("https://example.com", tmp_path / "merchants.yaml")

        assert exc_info.value.code == 0
        data = json.loads(capsys.readouterr().out)
        assert "warning" in data


# ---------------------------------------------------------------------------
# merchants.yaml persistence
# ---------------------------------------------------------------------------


class TestMerchantsYamlPersistence:
    def test_creates_merchants_yaml(self, monkeypatch, tmp_path, capsys):
        merchants_path = tmp_path / "merchants.yaml"
        assert not merchants_path.exists()

        _mock_public_dns(monkeypatch)
        with respx.mock:
            respx.get("https://example.com/.well-known/commerce.txt").mock(
                return_value=httpx.Response(200, text="UCP-Endpoint: https://api.example.com/ucp\n")
            )
            respx.get("https://api.example.com/ucp/capabilities").mock(
                return_value=httpx.Response(200, json={})
            )
            with pytest.raises(SystemExit):
                run_merchant_add_command("https://example.com", merchants_path)

        assert merchants_path.exists()

    def test_merchant_written_to_yaml(self, monkeypatch, tmp_path, capsys):
        merchants_path = tmp_path / "merchants.yaml"

        _mock_public_dns(monkeypatch)
        with respx.mock:
            respx.get("https://example.com/.well-known/commerce.txt").mock(
                return_value=httpx.Response(
                    200, text="UCP-Endpoint: https://api.example.com/ucp\nName: Example Store\n"
                )
            )
            respx.get("https://api.example.com/ucp/capabilities").mock(
                return_value=httpx.Response(200, json={})
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
        # Pre-seed with one merchant
        with merchants_path.open("w") as f:
            yaml.dump({"merchants": [{"slug": "existing", "name": "Existing", "adapter": "mock"}]}, f)

        _mock_public_dns(monkeypatch)
        with respx.mock:
            respx.get("https://example.com/.well-known/commerce.txt").mock(
                return_value=httpx.Response(200, text="UCP-Endpoint: https://api.example.com/ucp\n")
            )
            respx.get("https://api.example.com/ucp/capabilities").mock(
                return_value=httpx.Response(200, json={})
            )
            with pytest.raises(SystemExit):
                run_merchant_add_command("https://example.com", merchants_path)

        with merchants_path.open() as f:
            saved = yaml.safe_load(f)

        slugs = [m["slug"] for m in saved["merchants"]]
        assert "existing" in slugs
        assert "example-com" in slugs

    def test_duplicate_slug_is_upserted(self, monkeypatch, tmp_path, capsys):
        """Re-adding the same URL updates the entry rather than duplicating it."""
        merchants_path = tmp_path / "merchants.yaml"
        with merchants_path.open("w") as f:
            yaml.dump(
                {"merchants": [{"slug": "example-com", "name": "Old Name", "adapter": "ucp"}]}, f
            )

        _mock_public_dns(monkeypatch)
        with respx.mock:
            respx.get("https://example.com/.well-known/commerce.txt").mock(
                return_value=httpx.Response(
                    200, text="UCP-Endpoint: https://api.example.com/ucp\nName: New Name\n"
                )
            )
            respx.get("https://api.example.com/ucp/capabilities").mock(
                return_value=httpx.Response(200, json={})
            )
            with pytest.raises(SystemExit):
                run_merchant_add_command("https://example.com", merchants_path)

        with merchants_path.open() as f:
            saved = yaml.safe_load(f)

        assert len(saved["merchants"]) == 1
        assert saved["merchants"][0]["name"] == "New Name"


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
            socket, "getaddrinfo",
            lambda host, port, *args, **kwargs: _PUBLIC_ADDR,
        )
        with (
            patch("shop.commands.merchant.MERCHANTS_PATH", tmp_path / "merchants.yaml"),
            respx.mock,
        ):
            respx.get("https://example.com/.well-known/commerce.txt").mock(
                return_value=httpx.Response(
                    200, text="UCP-Endpoint: https://api.example.com/ucp\n"
                )
            )
            respx.get("https://api.example.com/ucp/capabilities").mock(
                return_value=httpx.Response(200, json={})
            )

            result = runner.invoke(app, ["merchant", "add", "https://example.com"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "added"
