"""shop history — transaction audit log."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import typer

from shop.config import SHOP_DIR
from shop.db import get_db

app = typer.Typer()


def _emit(data: dict, exit_code: int = 0) -> None:
    print(json.dumps(data, ensure_ascii=False))
    sys.exit(exit_code)


def history_command(
    last: int = typer.Option(20, "--last"),
    merchant: str | None = typer.Option(None, "--merchant"),
    shop_dir: Path = SHOP_DIR,
) -> None:
    run_history_command(last=last, merchant=merchant, shop_dir=shop_dir)


def run_history_command(
    last: int = 20,
    merchant: str | None = None,
    shop_dir: Path = SHOP_DIR,
) -> None:
    conn = get_db(shop_dir)

    if merchant:
        rows = conn.execute(
            """
            SELECT order_id, timestamp, sku, merchant, price_usd, mandate_id, status, exit_code
            FROM orders
            WHERE merchant = ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (merchant, last),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT order_id, timestamp, sku, merchant, price_usd, mandate_id, status, exit_code
            FROM orders
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (last,),
        ).fetchall()

    conn.close()

    orders = [
        {
            "timestamp": datetime.fromtimestamp(row["timestamp"], tz=timezone.utc).isoformat(),
            "order_id": row["order_id"],
            "sku": row["sku"],
            "merchant": row["merchant"],
            "price_usd": row["price_usd"],
            "mandate_id": row["mandate_id"],
            "status": row["status"],
            "exit_code": row["exit_code"],
        }
        for row in rows
    ]

    _emit({"orders": orders, "total": len(orders)})
