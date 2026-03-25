"""shop cart — add/view/clear cart items."""

from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

import typer

from shop.config import MERCHANTS_PATH, SHOP_DIR, create_adapter, load_config, load_merchants
from shop.db import get_db
from shop.mandate_utils import (
    MandateNotFoundError,
    check_mandate_policy,
    compute_period_start,
    get_period_spend,
    load_mandate,
)
from shop import scoring

app = typer.Typer()


def _emit(data: dict, exit_code: int = 0) -> None:
    print(json.dumps(data, ensure_ascii=False))
    sys.exit(exit_code)


def _error(error_code: str, detail: str, exit_code: int) -> None:
    _emit({"error_code": error_code, "detail": detail, "exit_code": exit_code}, exit_code)


async def _cart_add_async(
    sku: str,
    quantity: int,
    session_id: Optional[str],
    dry_run: bool,
    idempotency_key: Optional[str],
    shop_dir: Path,
    merchants_path: Path,
) -> None:
    merchant_slug = sku.split(":")[0]
    merchants = load_merchants(merchants_path)
    merchant = next((m for m in merchants if m.slug == merchant_slug), None)
    if not merchant:
        _error("merchant_not_found", f"Merchant not configured: {merchant_slug}", 4)

    adapter = create_adapter(merchant)
    try:
        product = await adapter.get_product(sku)
    except Exception as e:
        _error("product_not_found", str(e), 4)

    price_usd = product.price * quantity

    if not session_id:
        session_id = f"sess_{uuid.uuid4().hex[:8]}"

    cfg = load_config(shop_dir / "config.yaml")
    mandate_id = cfg.default_mandate
    mandate_check = "pass"
    budget_remaining_after = None

    if mandate_id:
        try:
            mandate = load_mandate(mandate_id, shop_dir / "mandates")
            policy_err = check_mandate_policy(mandate, merchant_slug, None, price_usd)
            if policy_err:
                mandate_check = "fail"
            else:
                conn = get_db(shop_dir)
                period = mandate.get("budget", {}).get("period", "monthly")
                period_start = compute_period_start(period)
                spent = get_period_spend(conn, mandate_id, period_start)
                conn.close()
                total = mandate.get("budget", {}).get("total_usd", 0.0)
                budget_remaining_after = round(max(0.0, total - spent - price_usd), 2)
        except MandateNotFoundError:
            mandate_id = None

    confidence, _ = scoring.score(product)

    if dry_run:
        _emit({
            "dry_run": True,
            "sku": sku,
            "merchant": merchant_slug,
            "quantity": quantity,
            "price_usd": price_usd,
            "mandate_check": mandate_check,
            "mandate_id": mandate_id,
            "budget_remaining_after": budget_remaining_after,
            "confidence": confidence,
            "warnings": [],
        })

    ik = idempotency_key or f"{session_id}_sku_{sku}"
    now_ts = int(time.time())

    conn = get_db(shop_dir)
    conn.execute(
        """
        INSERT OR REPLACE INTO cart_items (session_id, sku, merchant, quantity, price_usd, added_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (session_id, sku, merchant_slug, quantity, price_usd, now_ts),
    )
    conn.commit()

    totals = conn.execute(
        """
        SELECT SUM(price_usd) as cart_total, COUNT(*) as item_count
        FROM cart_items WHERE session_id = ?
        """,
        (session_id,),
    ).fetchone()
    conn.close()

    _emit({
        "session_id": session_id,
        "sku": sku,
        "merchant": merchant_slug,
        "quantity": quantity,
        "price_usd": price_usd,
        "cart_total": round(float(totals["cart_total"]), 2),
        "item_count": totals["item_count"],
        "idempotency_key": ik,
    })


@app.command("add")
def cart_add(
    sku: str = typer.Option(..., "--sku"),
    quantity: int = typer.Option(1, "--quantity"),
    session_id: Optional[str] = typer.Option(None, "--session-id"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    idempotency_key: Optional[str] = typer.Option(None, "--idempotency-key"),
    shop_dir: Path = SHOP_DIR,
    merchants_path: Path = MERCHANTS_PATH,
) -> None:
    run_cart_add_command(
        sku=sku,
        quantity=quantity,
        session_id=session_id,
        dry_run=dry_run,
        idempotency_key=idempotency_key,
        shop_dir=shop_dir,
        merchants_path=merchants_path,
    )


def run_cart_add_command(
    sku: str,
    quantity: int = 1,
    session_id: Optional[str] = None,
    dry_run: bool = False,
    idempotency_key: Optional[str] = None,
    shop_dir: Path = SHOP_DIR,
    merchants_path: Path = MERCHANTS_PATH,
) -> None:
    asyncio.run(_cart_add_async(sku, quantity, session_id, dry_run, idempotency_key, shop_dir, merchants_path))


@app.command("view")
def cart_view(
    session_id: Optional[str] = typer.Option(None, "--session-id"),
    shop_dir: Path = SHOP_DIR,
) -> None:
    run_cart_view_command(session_id=session_id, shop_dir=shop_dir)


def run_cart_view_command(session_id: Optional[str] = None, shop_dir: Path = SHOP_DIR) -> None:
    conn = get_db(shop_dir)

    if not session_id:
        row = conn.execute(
            "SELECT session_id FROM cart_items ORDER BY added_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            conn.close()
            _emit({"session_id": None, "items": [], "cart_total": 0.0, "item_count": 0, "created_at": None})
        session_id = row["session_id"]

    items_rows = conn.execute(
        "SELECT sku, merchant, quantity, price_usd FROM cart_items WHERE session_id = ?",
        (session_id,),
    ).fetchall()

    earliest = conn.execute(
        "SELECT MIN(added_at) as min_added FROM cart_items WHERE session_id = ?",
        (session_id,),
    ).fetchone()

    conn.close()

    from datetime import UTC, datetime
    items = [
        {"sku": r["sku"], "merchant": r["merchant"], "quantity": r["quantity"], "price_usd": r["price_usd"]}
        for r in items_rows
    ]
    cart_total = sum(i["price_usd"] for i in items)
    created_at = None
    if earliest and earliest["min_added"]:
        created_at = datetime.fromtimestamp(earliest["min_added"], tz=UTC).isoformat()

    _emit({
        "session_id": session_id,
        "items": items,
        "cart_total": round(cart_total, 2),
        "item_count": len(items),
        "created_at": created_at,
    })


@app.command("clear")
def cart_clear(
    session_id: Optional[str] = typer.Option(None, "--session-id"),
    yes: bool = typer.Option(..., "--yes", "-y"),
    shop_dir: Path = SHOP_DIR,
) -> None:
    run_cart_clear_command(session_id=session_id, yes=yes, shop_dir=shop_dir)


def run_cart_clear_command(
    session_id: Optional[str] = None,
    yes: bool = False,
    shop_dir: Path = SHOP_DIR,
) -> None:
    if not yes:
        _error("confirmation_required", "Pass --yes to confirm clearing the cart", 1)

    conn = get_db(shop_dir)

    if not session_id:
        row = conn.execute(
            "SELECT session_id FROM cart_items ORDER BY added_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            conn.close()
            _emit({"session_id": None, "cleared": True, "items_removed": 0})
        session_id = row["session_id"]

    count_row = conn.execute(
        "SELECT COUNT(*) as cnt FROM cart_items WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    items_removed = count_row["cnt"] if count_row else 0

    conn.execute("DELETE FROM cart_items WHERE session_id = ?", (session_id,))
    conn.commit()
    conn.close()

    _emit({"session_id": session_id, "cleared": True, "items_removed": items_removed})
