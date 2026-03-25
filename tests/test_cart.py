"""Tests for shop cart commands."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from shop.commands.cart import (
    run_cart_add_command,
    run_cart_clear_command,
    run_cart_view_command,
)
from shop.commands.mandate import run_mandate_create_command
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
    from datetime import UTC, datetime
    return CommerceTXTProduct(
        sku="mock:coffee-filters-100",
        title="Premium Cone Coffee Filters, 100ct",
        description="Unbleached cone filters.",
        price=price,
        availability="InStock",
        shipping=CommerceTXTShipping(cost=0.00, window_days="2-4", carrier="USPS"),
        returns=CommerceTXTReturns(window_days=30, condition="unopened", refund_timeline_days=5),
        trust=CommerceTXTTrust(seller_rating=4.7, review_count=312, certifications=[]),
        cached_at=datetime.now(UTC).isoformat(),
        price_history_30d={"min": 8.99, "max": 9.49},
    )


class TestCartAddDryRun:
    def test_cart_add_dry_run_no_mandate(self, tmp_path, capsys):
        shop_dir = tmp_path / "shop"
        merchants_path = _make_merchants_yaml(tmp_path)
        product = _make_product()

        mock_adapter = AsyncMock()
        mock_adapter.get_product = AsyncMock(return_value=product)

        with patch("shop.commands.cart.create_adapter", return_value=mock_adapter):
            with pytest.raises(SystemExit) as exc:
                run_cart_add_command(
                    sku="mock:coffee-filters-100",
                    dry_run=True,
                    shop_dir=shop_dir,
                    merchants_path=merchants_path,
                )
        assert exc.value.code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["dry_run"] is True
        assert data["mandate_check"] == "pass"
        assert data["mandate_id"] is None
        assert data["budget_remaining_after"] is None
        assert data["sku"] == "mock:coffee-filters-100"

    def test_cart_add_dry_run_with_mandate_pass(self, tmp_path, capsys):
        shop_dir = tmp_path / "shop"
        merchants_path = _make_merchants_yaml(tmp_path)
        product = _make_product(8.99)

        # Create a mandate
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
        mandate_data = json.loads(capsys.readouterr().out)
        mandate_id = mandate_data["mandate_id"]

        # Write config with default_mandate
        config_path = shop_dir / "config.yaml"
        config_path.write_text(f"default_mandate: {mandate_id}\n")

        mock_adapter = AsyncMock()
        mock_adapter.get_product = AsyncMock(return_value=product)

        with patch("shop.commands.cart.create_adapter", return_value=mock_adapter):
            with pytest.raises(SystemExit) as exc:
                run_cart_add_command(
                    sku="mock:coffee-filters-100",
                    dry_run=True,
                    shop_dir=shop_dir,
                    merchants_path=merchants_path,
                )
        assert exc.value.code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["dry_run"] is True
        assert data["mandate_check"] == "pass"
        assert data["mandate_id"] == mandate_id
        assert data["budget_remaining_after"] is not None


class TestCartAddCommit:
    def test_cart_add_commit_creates_db_row(self, tmp_path, capsys):
        shop_dir = tmp_path / "shop"
        merchants_path = _make_merchants_yaml(tmp_path)
        product = _make_product()

        mock_adapter = AsyncMock()
        mock_adapter.get_product = AsyncMock(return_value=product)

        with patch("shop.commands.cart.create_adapter", return_value=mock_adapter):
            with pytest.raises(SystemExit):
                run_cart_add_command(
                    sku="mock:coffee-filters-100",
                    session_id="sess_test123",
                    shop_dir=shop_dir,
                    merchants_path=merchants_path,
                )
        capsys.readouterr()

        conn = get_db(shop_dir)
        row = conn.execute(
            "SELECT * FROM cart_items WHERE session_id = 'sess_test123'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["sku"] == "mock:coffee-filters-100"

    def test_cart_add_commit_returns_session_id(self, tmp_path, capsys):
        shop_dir = tmp_path / "shop"
        merchants_path = _make_merchants_yaml(tmp_path)
        product = _make_product()

        mock_adapter = AsyncMock()
        mock_adapter.get_product = AsyncMock(return_value=product)

        with patch("shop.commands.cart.create_adapter", return_value=mock_adapter):
            with pytest.raises(SystemExit) as exc:
                run_cart_add_command(
                    sku="mock:coffee-filters-100",
                    shop_dir=shop_dir,
                    merchants_path=merchants_path,
                )
        assert exc.value.code == 0
        data = json.loads(capsys.readouterr().out)
        assert "session_id" in data
        assert data["session_id"].startswith("sess_")
        assert data["item_count"] == 1
        assert data["cart_total"] == 8.99


class TestCartView:
    def test_cart_view_returns_items(self, tmp_path, capsys):
        shop_dir = tmp_path / "shop"
        merchants_path = _make_merchants_yaml(tmp_path)
        product = _make_product()

        mock_adapter = AsyncMock()
        mock_adapter.get_product = AsyncMock(return_value=product)

        with patch("shop.commands.cart.create_adapter", return_value=mock_adapter):
            with pytest.raises(SystemExit):
                run_cart_add_command(
                    sku="mock:coffee-filters-100",
                    session_id="sess_view",
                    shop_dir=shop_dir,
                    merchants_path=merchants_path,
                )
        capsys.readouterr()

        with pytest.raises(SystemExit) as exc:
            run_cart_view_command(session_id="sess_view", shop_dir=shop_dir)
        assert exc.value.code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["session_id"] == "sess_view"
        assert data["item_count"] == 1
        assert len(data["items"]) == 1
        assert data["items"][0]["sku"] == "mock:coffee-filters-100"

    def test_cart_view_empty_with_no_session(self, tmp_path, capsys):
        shop_dir = tmp_path / "shop"

        with pytest.raises(SystemExit) as exc:
            run_cart_view_command(shop_dir=shop_dir)
        assert exc.value.code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["session_id"] is None
        assert data["items"] == []
        assert data["cart_total"] == 0.0
        assert data["item_count"] == 0


class TestCartClear:
    def test_cart_clear_removes_items(self, tmp_path, capsys):
        shop_dir = tmp_path / "shop"
        merchants_path = _make_merchants_yaml(tmp_path)
        product = _make_product()

        mock_adapter = AsyncMock()
        mock_adapter.get_product = AsyncMock(return_value=product)

        with patch("shop.commands.cart.create_adapter", return_value=mock_adapter):
            with pytest.raises(SystemExit):
                run_cart_add_command(
                    sku="mock:coffee-filters-100",
                    session_id="sess_clear",
                    shop_dir=shop_dir,
                    merchants_path=merchants_path,
                )
        capsys.readouterr()

        with pytest.raises(SystemExit) as exc:
            run_cart_clear_command(session_id="sess_clear", yes=True, shop_dir=shop_dir)
        assert exc.value.code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["cleared"] is True
        assert data["items_removed"] == 1

        conn = get_db(shop_dir)
        count = conn.execute(
            "SELECT COUNT(*) as cnt FROM cart_items WHERE session_id = 'sess_clear'"
        ).fetchone()["cnt"]
        conn.close()
        assert count == 0

    def test_cart_clear_requires_yes_flag(self, tmp_path, capsys):
        shop_dir = tmp_path / "shop"

        with pytest.raises(SystemExit) as exc:
            run_cart_clear_command(session_id="sess_x", yes=False, shop_dir=shop_dir)
        assert exc.value.code == 1
        data = json.loads(capsys.readouterr().out)
        assert data["error_code"] == "confirmation_required"
