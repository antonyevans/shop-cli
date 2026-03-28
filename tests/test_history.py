"""Tests for shop history command."""

from __future__ import annotations

import json
import time

import pytest

from shop.commands.history import run_history_command
from shop.db import get_db


def _insert_order(
    conn, order_id, sku, merchant, price, status="confirmed", exit_code=0, mandate_id=None, ts=None
):
    ts = ts or int(time.time())
    conn.execute(
        """
        INSERT INTO orders (order_id, timestamp, sku, merchant, price_usd, mandate_id,
                            status, exit_code, idempotency_key)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (order_id, ts, sku, merchant, price, mandate_id, status, exit_code, f"ik-{order_id}"),
    )
    conn.commit()


class TestHistory:
    def test_history_empty(self, tmp_path, capsys):
        shop_dir = tmp_path / "shop"

        with pytest.raises(SystemExit) as exc:
            run_history_command(shop_dir=shop_dir)
        assert exc.value.code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["orders"] == []
        assert data["total"] == 0

    def test_history_shows_orders(self, tmp_path, capsys):
        shop_dir = tmp_path / "shop"
        conn = get_db(shop_dir)
        _insert_order(conn, "ORD-001", "mock:coffee-filters-100", "mock", 8.99, mandate_id="m1")
        _insert_order(conn, "ORD-002", "mock:dish-soap-blue", "mock", 5.49, mandate_id="m1")
        conn.close()

        with pytest.raises(SystemExit) as exc:
            run_history_command(shop_dir=shop_dir)
        assert exc.value.code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["total"] == 2
        order_ids = {o["order_id"] for o in data["orders"]}
        assert "ORD-001" in order_ids
        assert "ORD-002" in order_ids

    def test_history_last_n_limit(self, tmp_path, capsys):
        shop_dir = tmp_path / "shop"
        conn = get_db(shop_dir)
        for i in range(5):
            _insert_order(conn, f"ORD-{i:03}", f"mock:sku-{i}", "mock", 1.0 + i, ts=1000 + i)
        conn.close()

        with pytest.raises(SystemExit) as exc:
            run_history_command(last=3, shop_dir=shop_dir)
        assert exc.value.code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["total"] == 3

    def test_history_merchant_filter(self, tmp_path, capsys):
        shop_dir = tmp_path / "shop"
        conn = get_db(shop_dir)
        _insert_order(conn, "ORD-A01", "mock:sku-1", "mock", 8.99)
        _insert_order(conn, "ORD-B01", "other:sku-2", "other", 5.00)
        _insert_order(conn, "ORD-A02", "mock:sku-3", "mock", 12.00)
        conn.close()

        with pytest.raises(SystemExit) as exc:
            run_history_command(merchant="mock", shop_dir=shop_dir)
        assert exc.value.code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["total"] == 2
        for order in data["orders"]:
            assert order["merchant"] == "mock"
