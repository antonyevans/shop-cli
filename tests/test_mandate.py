"""Tests for shop mandate commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from shop.commands.mandate import (
    run_mandate_create_command,
    run_mandate_list_command,
    run_mandate_usage_command,
    run_mandate_verify_command,
)
from shop.mandate_utils import (
    load_mandate,
    verify_mandate,
)


def _create_mandate(tmp_path: Path, **kwargs) -> dict:
    defaults = dict(
        budget_total=100.0,
        per_order_max=25.0,
        period="monthly",
        category_allow=None,
        category_deny=None,
        merchant_allow=None,
        merchant_deny=None,
        expires_at=None,
        shop_dir=tmp_path / "shop",
    )
    defaults.update(kwargs)
    with pytest.raises(SystemExit) as exc:
        run_mandate_create_command(**defaults)
    assert exc.value.code == 0
    return exc


class TestMandateCreate:
    def test_create_mandate_returns_valid_json(self, tmp_path, capsys):
        shop_dir = tmp_path / "shop"
        with pytest.raises(SystemExit) as exc:
            run_mandate_create_command(
                budget_total=100.0,
                per_order_max=25.0,
                period="monthly",
                category_allow=None,
                category_deny=None,
                merchant_allow=None,
                merchant_deny=None,
                expires_at=None,
                shop_dir=shop_dir,
            )
        assert exc.value.code == 0
        data = json.loads(capsys.readouterr().out)
        assert "mandate_id" in data
        assert data["signature_valid"] is True
        assert "file_path" in data

    def test_mandate_signature_is_valid(self, tmp_path, capsys):
        shop_dir = tmp_path / "shop"
        with pytest.raises(SystemExit):
            run_mandate_create_command(
                budget_total=50.0,
                per_order_max=10.0,
                period="weekly",
                category_allow=None,
                category_deny=None,
                merchant_allow=None,
                merchant_deny=None,
                expires_at=None,
                shop_dir=shop_dir,
            )
        data = json.loads(capsys.readouterr().out)
        mandate_id = data["mandate_id"]
        mandate = load_mandate(mandate_id, shop_dir / "mandates")
        assert verify_mandate(mandate) is True

    def test_mandate_create_writes_yaml_file(self, tmp_path, capsys):
        shop_dir = tmp_path / "shop"
        with pytest.raises(SystemExit):
            run_mandate_create_command(
                budget_total=200.0,
                per_order_max=50.0,
                period="one-time",
                category_allow=None,
                category_deny=None,
                merchant_allow=None,
                merchant_deny=None,
                expires_at=None,
                shop_dir=shop_dir,
            )
        data = json.loads(capsys.readouterr().out)
        file_path = Path(data["file_path"])
        assert file_path.exists()
        with file_path.open() as f:
            content = yaml.safe_load(f)
        assert content["mandate_id"] == data["mandate_id"]
        assert content["budget"]["total_usd"] == 200.0


class TestMandateList:
    def test_mandate_list_returns_mandates(self, tmp_path, capsys):
        shop_dir = tmp_path / "shop"
        # Create 2 mandates
        for _ in range(2):
            with pytest.raises(SystemExit):
                run_mandate_create_command(
                    budget_total=100.0,
                    per_order_max=25.0,
                    period="monthly",
                    category_allow=None,
                    category_deny=None,
                    merchant_allow=None,
                    merchant_deny=None,
                    expires_at=None,
                    shop_dir=shop_dir,
                )
            capsys.readouterr()  # drain

        with pytest.raises(SystemExit) as exc:
            run_mandate_list_command(shop_dir=shop_dir)
        assert exc.value.code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["total"] == 2
        assert len(data["mandates"]) == 2
        for m in data["mandates"]:
            assert "mandate_id" in m
            assert m["status"] == "active"
            assert m["signature_valid"] is True


class TestMandateVerify:
    def test_mandate_verify_valid_mandate(self, tmp_path, capsys):
        shop_dir = tmp_path / "shop"
        with pytest.raises(SystemExit):
            run_mandate_create_command(
                budget_total=100.0,
                per_order_max=25.0,
                period="monthly",
                category_allow=None,
                category_deny=None,
                merchant_allow=None,
                merchant_deny=None,
                expires_at=None,
                shop_dir=shop_dir,
            )
        created = json.loads(capsys.readouterr().out)
        mandate_id = created["mandate_id"]

        with pytest.raises(SystemExit) as exc:
            run_mandate_verify_command(mandate_id=mandate_id, shop_dir=shop_dir)
        assert exc.value.code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["signature_valid"] is True
        assert data["tamper_detected"] is False
        assert data["expired"] is False

    def test_mandate_verify_tampered_mandate(self, tmp_path, capsys):
        shop_dir = tmp_path / "shop"
        with pytest.raises(SystemExit):
            run_mandate_create_command(
                budget_total=100.0,
                per_order_max=25.0,
                period="monthly",
                category_allow=None,
                category_deny=None,
                merchant_allow=None,
                merchant_deny=None,
                expires_at=None,
                shop_dir=shop_dir,
            )
        created = json.loads(capsys.readouterr().out)
        mandate_id = created["mandate_id"]

        # Tamper with the file
        mandate_path = shop_dir / "mandates" / f"{mandate_id}.yaml"
        with mandate_path.open() as f:
            content = yaml.safe_load(f)
        content["budget"]["total_usd"] = 9999.0
        with mandate_path.open("w") as f:
            yaml.dump(content, f)

        with pytest.raises(SystemExit) as exc:
            run_mandate_verify_command(mandate_id=mandate_id, shop_dir=shop_dir)
        assert exc.value.code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["signature_valid"] is False
        assert data["tamper_detected"] is True


class TestMandateUsage:
    def test_mandate_usage_empty_spend(self, tmp_path, capsys):
        shop_dir = tmp_path / "shop"
        with pytest.raises(SystemExit):
            run_mandate_create_command(
                budget_total=100.0,
                per_order_max=25.0,
                period="monthly",
                category_allow=None,
                category_deny=None,
                merchant_allow=None,
                merchant_deny=None,
                expires_at=None,
                shop_dir=shop_dir,
            )
        created = json.loads(capsys.readouterr().out)
        mandate_id = created["mandate_id"]

        with pytest.raises(SystemExit) as exc:
            run_mandate_usage_command(mandate_id=mandate_id, shop_dir=shop_dir)
        assert exc.value.code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["budget_total"] == 100.0
        assert data["budget_used"] == 0.0
        assert data["budget_remaining"] == 100.0
        assert data["per_category_spend"] == []
        assert data["pending_orders"] == []

    def test_mandate_expired_status(self, tmp_path, capsys):
        shop_dir = tmp_path / "shop"
        # Create a mandate with expiry in the past
        with pytest.raises(SystemExit):
            run_mandate_create_command(
                budget_total=100.0,
                per_order_max=25.0,
                period="monthly",
                category_allow=None,
                category_deny=None,
                merchant_allow=None,
                merchant_deny=None,
                expires_at="2020-01-01T00:00:00+00:00",
                shop_dir=shop_dir,
            )
        capsys.readouterr()

        with pytest.raises(SystemExit) as exc:
            run_mandate_list_command(shop_dir=shop_dir)
        assert exc.value.code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["total"] == 1
        assert data["mandates"][0]["status"] == "expired"
