"""Tests for shop order commands."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from shop.commands.mandate import run_mandate_create_command
from shop.commands.order import run_order_create_command, run_order_status_command
from shop.db import get_db
from shop.models.commerce import (
    CommerceTXTProduct,
    CommerceTXTReturns,
    CommerceTXTShipping,
    CommerceTXTTrust,
)


def _make_merchants_yaml(tmp_path: Path) -> Path:
    merchants_path = tmp_path / "merchants.yaml"
    merchants_path.write_text(
        "merchants:\n"
        "  - slug: mock\n"
        "    name: Mock Store\n"
        "    adapter: mock\n"
    )
    return merchants_path


def _make_product(price: float = 8.99) -> CommerceTXTProduct:
    return CommerceTXTProduct(
        sku="mock:coffee-filters-100",
        title="Coffee Filters",
        price=price,
        availability="InStock",
        shipping=CommerceTXTShipping(cost=0.00, window_days="2-4"),
        returns=CommerceTXTReturns(window_days=30),
        trust=CommerceTXTTrust(seller_rating=4.7, review_count=100),
        cached_at=datetime.now(UTC).isoformat(),
    )


def _create_mandate(tmp_path: Path, capsys, **kwargs) -> str:
    shop_dir = tmp_path / "shop"
    defaults = dict(
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
    defaults.update(kwargs)
    with pytest.raises(SystemExit):
        run_mandate_create_command(**defaults)
    data = json.loads(capsys.readouterr().out)
    return data["mandate_id"]


def _mock_order_result(sku: str, price: float) -> dict:
    return {
        "order_id": "MOCK-TESTORDER",
        "status": "confirmed",
        "sku": sku,
        "quantity": 1,
        "price_usd": price,
        "merchant": "mock",
        "mandate_id": "test-mandate",
        "idempotency_key": "test-key",
        "tracking": {"carrier": None, "tracking_number": None, "estimated_delivery": None},
    }


class TestOrderCreateSuccess:
    def test_order_create_success_single_sku(self, tmp_path, capsys):
        shop_dir = tmp_path / "shop"
        merchants_path = _make_merchants_yaml(tmp_path)
        mandate_id = _create_mandate(tmp_path, capsys)

        product = _make_product(8.99)
        mock_adapter = AsyncMock()
        mock_adapter.get_product = AsyncMock(return_value=product)
        mock_adapter.create_order = AsyncMock(return_value=_mock_order_result("mock:coffee-filters-100", 8.99))

        with patch("shop.commands.order.create_adapter", return_value=mock_adapter):
            with pytest.raises(SystemExit) as exc:
                run_order_create_command(
                    sku="mock:coffee-filters-100",
                    quantity=1,
                    from_cart=False,
                    mandate_id=mandate_id,
                    idempotency_key="test-key-001",
                    yes=True,
                    shop_dir=shop_dir,
                    merchants_path=merchants_path,
                )
        assert exc.value.code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["total_orders"] == 1
        assert len(data["orders"]) == 1
        order = data["orders"][0]
        assert order["status"] == "confirmed"
        assert order["mandate_id"] == mandate_id
        assert order["sku"] == "mock:coffee-filters-100"
        assert "tracking" in order


class TestOrderMandateViolations:
    def test_order_create_mandate_violation_budget_exhausted(self, tmp_path, capsys):
        shop_dir = tmp_path / "shop"
        merchants_path = _make_merchants_yaml(tmp_path)
        mandate_id = _create_mandate(tmp_path, capsys, budget_total=5.0, per_order_max=100.0)

        product = _make_product(8.99)
        mock_adapter = AsyncMock()
        mock_adapter.get_product = AsyncMock(return_value=product)
        mock_adapter.create_order = AsyncMock(return_value=_mock_order_result("mock:coffee-filters-100", 8.99))

        with patch("shop.commands.order.create_adapter", return_value=mock_adapter):
            with pytest.raises(SystemExit) as exc:
                run_order_create_command(
                    sku="mock:coffee-filters-100",
                    quantity=1,
                    from_cart=False,
                    mandate_id=mandate_id,
                    idempotency_key="test-budget-key",
                    yes=True,
                    shop_dir=shop_dir,
                    merchants_path=merchants_path,
                )
        assert exc.value.code == 3
        data = json.loads(capsys.readouterr().out)
        assert data["error_code"] == "budget_exhausted"

    def test_order_create_per_order_limit_exceeded(self, tmp_path, capsys):
        shop_dir = tmp_path / "shop"
        merchants_path = _make_merchants_yaml(tmp_path)
        mandate_id = _create_mandate(tmp_path, capsys, budget_total=100.0, per_order_max=5.0)

        product = _make_product(8.99)
        mock_adapter = AsyncMock()
        mock_adapter.get_product = AsyncMock(return_value=product)

        with patch("shop.commands.order.create_adapter", return_value=mock_adapter):
            with pytest.raises(SystemExit) as exc:
                run_order_create_command(
                    sku="mock:coffee-filters-100",
                    quantity=1,
                    from_cart=False,
                    mandate_id=mandate_id,
                    idempotency_key="test-per-order-key",
                    yes=True,
                    shop_dir=shop_dir,
                    merchants_path=merchants_path,
                )
        assert exc.value.code == 3
        data = json.loads(capsys.readouterr().out)
        assert data["error_code"] == "per_order_limit_exceeded"

    def test_order_create_mandate_expired(self, tmp_path, capsys):
        shop_dir = tmp_path / "shop"
        merchants_path = _make_merchants_yaml(tmp_path)
        mandate_id = _create_mandate(
            tmp_path, capsys, expires_at="2020-01-01T00:00:00+00:00"
        )

        product = _make_product(8.99)
        mock_adapter = AsyncMock()
        mock_adapter.get_product = AsyncMock(return_value=product)

        with patch("shop.commands.order.create_adapter", return_value=mock_adapter):
            with pytest.raises(SystemExit) as exc:
                run_order_create_command(
                    sku="mock:coffee-filters-100",
                    quantity=1,
                    from_cart=False,
                    mandate_id=mandate_id,
                    idempotency_key="test-expired-key",
                    yes=True,
                    shop_dir=shop_dir,
                    merchants_path=merchants_path,
                )
        assert exc.value.code == 3
        data = json.loads(capsys.readouterr().out)
        assert data["error_code"] == "mandate_expired"


class TestOrderValidation:
    def test_order_create_missing_idempotency_key_exits_1(self, tmp_path, capsys):
        shop_dir = tmp_path / "shop"
        merchants_path = _make_merchants_yaml(tmp_path)

        with pytest.raises(SystemExit) as exc:
            run_order_create_command(
                sku="mock:coffee-filters-100",
                yes=True,
                shop_dir=shop_dir,
                merchants_path=merchants_path,
            )
        assert exc.value.code == 1
        data = json.loads(capsys.readouterr().out)
        assert data["error_code"] == "missing_idempotency_key"

    def test_order_create_idempotency_key_deduplication(self, tmp_path, capsys):
        shop_dir = tmp_path / "shop"
        merchants_path = _make_merchants_yaml(tmp_path)
        mandate_id = _create_mandate(tmp_path, capsys)

        product = _make_product(8.99)
        mock_adapter = AsyncMock()
        mock_adapter.get_product = AsyncMock(return_value=product)
        mock_adapter.create_order = AsyncMock(return_value=_mock_order_result("mock:coffee-filters-100", 8.99))

        with patch("shop.commands.order.create_adapter", return_value=mock_adapter):
            with pytest.raises(SystemExit) as exc:
                run_order_create_command(
                    sku="mock:coffee-filters-100",
                    mandate_id=mandate_id,
                    idempotency_key="dedup-key-001",
                    yes=True,
                    shop_dir=shop_dir,
                    merchants_path=merchants_path,
                )
        assert exc.value.code == 0
        first_data = json.loads(capsys.readouterr().out)
        first_order_id = first_data["orders"][0]["order_id"]

        # Second call with same idempotency key
        with patch("shop.commands.order.create_adapter", return_value=mock_adapter):
            with pytest.raises(SystemExit) as exc:
                run_order_create_command(
                    sku="mock:coffee-filters-100",
                    mandate_id=mandate_id,
                    idempotency_key="dedup-key-001",
                    yes=True,
                    shop_dir=shop_dir,
                    merchants_path=merchants_path,
                )
        assert exc.value.code == 0
        second_data = json.loads(capsys.readouterr().out)
        second_order_id = second_data["orders"][0]["order_id"]

        assert first_order_id == second_order_id


class TestOrderStatus:
    def test_order_status_found(self, tmp_path, capsys):
        shop_dir = tmp_path / "shop"
        merchants_path = _make_merchants_yaml(tmp_path)
        mandate_id = _create_mandate(tmp_path, capsys)

        product = _make_product(8.99)
        mock_adapter = AsyncMock()
        mock_adapter.get_product = AsyncMock(return_value=product)
        mock_adapter.create_order = AsyncMock(return_value=_mock_order_result("mock:coffee-filters-100", 8.99))

        with patch("shop.commands.order.create_adapter", return_value=mock_adapter):
            with pytest.raises(SystemExit):
                run_order_create_command(
                    sku="mock:coffee-filters-100",
                    mandate_id=mandate_id,
                    idempotency_key="status-test-key",
                    yes=True,
                    shop_dir=shop_dir,
                    merchants_path=merchants_path,
                )
        data = json.loads(capsys.readouterr().out)
        order_id = data["orders"][0]["order_id"]

        with pytest.raises(SystemExit) as exc:
            run_order_status_command(order_id=order_id, shop_dir=shop_dir)
        assert exc.value.code == 0
        status_data = json.loads(capsys.readouterr().out)
        assert status_data["order_id"] == order_id
        assert status_data["status"] == "confirmed"
        assert "tracking" in status_data
        assert "created_at" in status_data

    def test_order_status_not_found_exits_4(self, tmp_path, capsys):
        shop_dir = tmp_path / "shop"

        with pytest.raises(SystemExit) as exc:
            run_order_status_command(order_id="ORD-NONEXISTENT", shop_dir=shop_dir)
        assert exc.value.code == 4
        data = json.loads(capsys.readouterr().out)
        assert data["error_code"] == "order_not_found"
