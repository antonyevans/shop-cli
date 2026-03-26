"""Tests for shop payment add/list commands."""

from __future__ import annotations

import json
import pytest
import yaml

from shop.commands.payment import run_payment_add_command, run_payment_list_command


class TestPaymentAdd:
    def test_add_saves_payment_yaml(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as exc_info:
            run_payment_add_command(
                label="Test Visa", number="4242424242424242",
                first_name="Test", last_name="User",
                month=12, year=2026, cvv="123",
                shop_dir=tmp_path,
            )
        assert exc_info.value.code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["status"] == "added"
        assert data["card_last4"] == "4242"

    def test_payment_yaml_created_with_600_perms(self, tmp_path, capsys):
        with pytest.raises(SystemExit):
            run_payment_add_command(
                label="My Card", number="4242424242424242",
                first_name="A", last_name="B",
                month=1, year=2026, cvv="000",
                shop_dir=tmp_path,
            )
        p = tmp_path / "payment.yaml"
        assert p.exists()
        assert oct(p.stat().st_mode)[-3:] == "600"

    def test_card_stored_in_yaml(self, tmp_path, capsys):
        with pytest.raises(SystemExit):
            run_payment_add_command(
                label="Dev Card", number="4242 4242 4242 4242",
                first_name="Jane", last_name="Doe",
                month=11, year=2027, cvv="321",
                address1="99 Test St", city="Boston", province="MA",
                country="US", zip_code="02101",
                shop_dir=tmp_path,
            )
        with (tmp_path / "payment.yaml").open() as f:
            saved = yaml.safe_load(f)

        method = saved["methods"][0]
        assert method["label"] == "Dev Card"
        assert method["number"] == "4242424242424242"  # spaces stripped
        assert method["first_name"] == "Jane"
        assert method["billing"]["city"] == "Boston"

    def test_invalid_card_number_exits_1(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as exc_info:
            run_payment_add_command(
                label="Bad", number="123",
                first_name="A", last_name="B",
                month=1, year=2026, cvv="000",
                shop_dir=tmp_path,
            )
        assert exc_info.value.code == 1
        data = json.loads(capsys.readouterr().out)
        assert data["error_code"] == "invalid_card"

    def test_invalid_month_exits_1(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as exc_info:
            run_payment_add_command(
                label="Bad", number="4242424242424242",
                first_name="A", last_name="B",
                month=13, year=2026, cvv="000",
                shop_dir=tmp_path,
            )
        assert exc_info.value.code == 1

    def test_first_added_becomes_default(self, tmp_path, capsys):
        with pytest.raises(SystemExit):
            run_payment_add_command(
                label="Primary", number="4242424242424242",
                first_name="A", last_name="B",
                month=1, year=2026, cvv="000",
                shop_dir=tmp_path,
            )
        data = json.loads(capsys.readouterr().out)
        assert data["default"] is True

    def test_duplicate_label_is_replaced(self, tmp_path, capsys):
        for n in ["4242424242424242", "5555555555554444"]:
            with pytest.raises(SystemExit):
                run_payment_add_command(
                    label="My Card", number=n,
                    first_name="A", last_name="B",
                    month=1, year=2026, cvv="000",
                    shop_dir=tmp_path,
                )
            capsys.readouterr()

        with (tmp_path / "payment.yaml").open() as f:
            saved = yaml.safe_load(f)
        assert len(saved["methods"]) == 1
        assert saved["methods"][0]["number"] == "5555555555554444"


class TestPaymentList:
    def test_empty_list(self, tmp_path, capsys):
        with pytest.raises(SystemExit):
            run_payment_list_command(shop_dir=tmp_path)
        data = json.loads(capsys.readouterr().out)
        assert data["methods"] == []
        assert data["count"] == 0

    def test_list_masks_card_number(self, tmp_path, capsys):
        with pytest.raises(SystemExit):
            run_payment_add_command(
                label="Visa", number="4242424242424242",
                first_name="A", last_name="B",
                month=6, year=2026, cvv="999",
                shop_dir=tmp_path,
            )
        capsys.readouterr()

        with pytest.raises(SystemExit):
            run_payment_list_command(shop_dir=tmp_path)
        data = json.loads(capsys.readouterr().out)
        assert data["count"] == 1
        assert data["methods"][0]["card_last4"] == "4242"
        assert data["methods"][0]["expiry"] == "6/2026"
