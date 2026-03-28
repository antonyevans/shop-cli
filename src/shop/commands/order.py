"""shop order — create and track orders."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer

from shop.adapters.base import CheckoutNotSupportedError
from shop.config import MERCHANTS_PATH, SHOP_DIR, create_adapter, load_config, load_merchants
from shop.db import get_db
from shop.mandate_utils import (
    MandateNotFoundError,
    check_mandate_policy,
    compute_period_start,
    load_mandate,
)

app = typer.Typer()


def _emit(data: dict, exit_code: int = 0) -> None:
    print(json.dumps(data, ensure_ascii=False))
    sys.exit(exit_code)


def _error(error_code: str, detail: str, exit_code: int) -> None:
    _emit({"error_code": error_code, "detail": detail, "exit_code": exit_code}, exit_code)


def _store_domain_from_url(checkout_url: str) -> Optional[str]:
    """Extract store domain (e.g. my-store.myshopify.com) from a Shopify checkout URL."""
    from urllib.parse import urlparse

    parsed = urlparse(checkout_url)
    return parsed.hostname


async def _place_one_order(
    sku: str,
    quantity: int,
    mandate_id: str,
    idempotency_key: str,
    shop_dir: Path,
    merchants_path: Path,
    checkout_url: Optional[str] = None,
    price_usd_override: Optional[float] = None,
) -> dict:
    merchant_slug = sku.split(":")[0]
    merchants = load_merchants(merchants_path)
    merchant = next((m for m in merchants if m.slug == merchant_slug), None)
    if not merchant:
        _error("merchant_not_found", f"Merchant not configured: {merchant_slug}", 4)

    mandate = load_mandate(mandate_id, shop_dir / "mandates")

    # Route Shopify catalog SKUs to the per-store Storefront adapter
    if merchant.adapter == "shopify_catalog" and checkout_url:
        store_domain = _store_domain_from_url(checkout_url)
        storefront_merchant = next(
            (
                m
                for m in merchants
                if m.adapter == "shopify_storefront" and m.extra.get("store_domain") == store_domain
            ),
            None,
        )
        if not storefront_merchant:
            _error(
                "store_not_registered",
                f"Shopify store {store_domain} is not registered for checkout. "
                f"Run: shop merchant add-shopify-store"
                f" --store-domain {store_domain} --storefront-token TOKEN",
                4,
            )
        merchant = storefront_merchant

    adapter = create_adapter(merchant)

    # Get product price — use override if adapter can't fetch detail
    if price_usd_override is not None:
        price_usd = price_usd_override * quantity
    else:
        try:
            product = await adapter.get_product(sku)
            price_usd = product.price * quantity
        except Exception:
            _error(
                "product_not_found",
                f"Cannot fetch price for {sku} — add to cart first or use --price-usd",
                4,
            )

    # Policy checks
    policy_err = check_mandate_policy(mandate, merchant_slug, None, price_usd)
    if policy_err:
        _error(policy_err, f"Mandate policy violation: {policy_err}", 3)

    conn = get_db(shop_dir)
    now_ts = int(time.time())

    # Phase 1 — budget check + reserve (BEGIN IMMEDIATE)
    order_id = f"ORD-{uuid.uuid4().hex[:10].upper()}"
    try:
        conn.execute("BEGIN IMMEDIATE")
        period = mandate.get("budget", {}).get("period", "monthly")
        period_start = compute_period_start(period)
        spent = float(
            conn.execute(
                """
            SELECT COALESCE(SUM(amount_usd), 0.0) as total
            FROM mandate_spend
            WHERE mandate_id = ? AND recorded_at >= ? AND status IN ('confirmed', 'pending')
            """,
                (mandate_id, period_start),
            ).fetchone()["total"]
        )
        total_budget = mandate.get("budget", {}).get("total_usd", 0.0)
        if (total_budget - spent) < price_usd:
            conn.execute("ROLLBACK")
            conn.close()
            _error("budget_exhausted", "Insufficient mandate budget remaining", 3)

        conn.execute(
            """
            INSERT INTO mandate_spend
            (mandate_id, order_id, amount_usd, category, recorded_at, status)
            VALUES (?, ?, ?, ?, ?, 'pending')
            """,
            (mandate_id, order_id, price_usd, None, now_ts),
        )
        conn.commit()
    except sqlite3.OperationalError as e:
        if "locked" in str(e).lower():
            conn.close()
            _error("database_busy", "Database is locked, please retry", 6)
        raise

    # Phase 2 — call merchant
    try:
        result = await adapter.create_order(
            sku, quantity, mandate_id, idempotency_key, checkout_url=checkout_url
        )
    except CheckoutNotSupportedError:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM mandate_spend WHERE order_id = ?", (order_id,))
        conn.execute(
            """
            INSERT INTO orders (order_id, timestamp, sku, merchant, price_usd, mandate_id,
                                status, exit_code, idempotency_key, raw_response)
            VALUES (?, ?, ?, ?, ?, ?, 'cancelled', 4, ?, NULL)
            """,
            (order_id, now_ts, sku, merchant_slug, price_usd, mandate_id, idempotency_key),
        )
        conn.commit()
        conn.close()
        _error("checkout_not_supported", "Merchant does not support order creation", 4)
    except Exception as exc:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM mandate_spend WHERE order_id = ?", (order_id,))
        conn.execute(
            """
            INSERT INTO orders (order_id, timestamp, sku, merchant, price_usd, mandate_id,
                                status, exit_code, idempotency_key, raw_response)
            VALUES (?, ?, ?, ?, ?, ?, 'cancelled', 6, ?, NULL)
            """,
            (order_id, now_ts, sku, merchant_slug, price_usd, mandate_id, idempotency_key),
        )
        conn.commit()
        conn.close()
        _error("order_failed", str(exc), 6)

    # Phase 3 — settle
    raw_resp_json = json.dumps(result)
    conn.execute("BEGIN")
    conn.execute(
        "UPDATE mandate_spend SET status = 'confirmed' WHERE order_id = ?",
        (order_id,),
    )
    try:
        conn.execute(
            """
            INSERT INTO orders (order_id, timestamp, sku, merchant, price_usd, mandate_id,
                                status, exit_code, idempotency_key, raw_response)
            VALUES (?, ?, ?, ?, ?, ?, 'confirmed', 0, ?, ?)
            """,
            (
                order_id,
                now_ts,
                sku,
                merchant_slug,
                price_usd,
                mandate_id,
                idempotency_key,
                raw_resp_json,
            ),
        )
    except sqlite3.IntegrityError:
        # Idempotency: read existing order
        conn.execute("ROLLBACK")
        existing = conn.execute(
            "SELECT * FROM orders WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        conn.close()
        if existing:
            tracking = {}
            if existing["raw_response"]:
                try:
                    tracking = json.loads(existing["raw_response"]).get("tracking", {})
                except Exception:
                    tracking = {}
            return {
                "order_id": existing["order_id"],
                "sku": existing["sku"],
                "merchant": existing["merchant"],
                "status": existing["status"],
                "price_usd": existing["price_usd"],
                "mandate_id": existing["mandate_id"],
                "idempotency_key": existing["idempotency_key"],
                "tracking": tracking
                or {"carrier": None, "tracking_number": None, "estimated_delivery": None},
            }
        _error("order_failed", "Idempotency conflict but order not found", 6)
    conn.commit()

    # Auto-purge
    now_s = int(time.time())
    conn.execute("DELETE FROM cart_items WHERE added_at < ?", (now_s - 86400,))
    conn.execute(
        "UPDATE orders SET raw_response = NULL WHERE timestamp < ?",
        (now_s - 30 * 86400,),
    )
    conn.commit()
    conn.close()

    tracking = result.get("tracking", {}) or {}
    return {
        "order_id": order_id,
        "sku": sku,
        "merchant": merchant_slug,
        "status": "confirmed",
        "price_usd": price_usd,
        "mandate_id": mandate_id,
        "idempotency_key": idempotency_key,
        "tracking": tracking
        or {"carrier": None, "tracking_number": None, "estimated_delivery": None},
    }


async def _run_order_create(
    sku: Optional[str],
    quantity: int,
    from_cart: bool,
    session_id: Optional[str],
    mandate_id: Optional[str],
    idempotency_key: str,
    shop_dir: Path,
    merchants_path: Path,
) -> None:
    if not mandate_id:
        cfg = load_config(shop_dir / "config.yaml")
        mandate_id = cfg.default_mandate
    if not mandate_id:
        _error("no_mandate", "No mandate specified and no default_mandate in config", 1)

    try:
        load_mandate(mandate_id, shop_dir / "mandates")
    except MandateNotFoundError:
        _error("mandate_not_found", f"Mandate not found: {mandate_id}", 3)

    if from_cart:
        conn = get_db(shop_dir)
        if not session_id:
            row = conn.execute(
                "SELECT session_id FROM cart_items ORDER BY added_at DESC LIMIT 1"
            ).fetchone()
            if not row:
                conn.close()
                _error("empty_cart", "No cart session found", 1)
            session_id = row["session_id"]

        items = conn.execute(
            "SELECT sku, quantity, price_usd, checkout_url FROM cart_items WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        conn.close()

        if not items:
            _error("empty_cart", "Cart session is empty", 1)

        orders = []
        total_amount = 0.0
        for i, item in enumerate(items):
            item_ik = f"{idempotency_key}_item_{i}"
            order = await _place_one_order(
                sku=item["sku"],
                quantity=item["quantity"],
                mandate_id=mandate_id,
                idempotency_key=item_ik,
                shop_dir=shop_dir,
                merchants_path=merchants_path,
                checkout_url=item["checkout_url"],
                price_usd_override=item["price_usd"] / item["quantity"],
            )
            orders.append(order)
            total_amount += order["price_usd"]

        # Clear cart after successful order
        conn = get_db(shop_dir)
        conn.execute("DELETE FROM cart_items WHERE session_id = ?", (session_id,))
        conn.commit()
        conn.close()

        _emit(
            {
                "orders": orders,
                "total_orders": len(orders),
                "total_amount_usd": round(total_amount, 2),
            }
        )
    else:
        order = await _place_one_order(
            sku=sku,
            quantity=quantity,
            mandate_id=mandate_id,
            idempotency_key=idempotency_key,
            shop_dir=shop_dir,
            merchants_path=merchants_path,
        )
        _emit(
            {
                "orders": [order],
                "total_orders": 1,
                "total_amount_usd": round(order["price_usd"], 2),
            }
        )


@app.command("create")
def order_create(
    sku: Optional[str] = typer.Option(None, "--sku"),
    quantity: int = typer.Option(1, "--quantity"),
    from_cart: bool = typer.Option(False, "--from-cart"),
    session_id: Optional[str] = typer.Option(None, "--session-id"),
    mandate_id: Optional[str] = typer.Option(None, "--mandate-id"),
    idempotency_key: str = typer.Option(..., "--idempotency-key"),
    yes: bool = typer.Option(..., "--yes", "-y"),
    shop_dir: Path = SHOP_DIR,
    merchants_path: Path = MERCHANTS_PATH,
) -> None:
    run_order_create_command(
        sku=sku,
        quantity=quantity,
        from_cart=from_cart,
        session_id=session_id,
        mandate_id=mandate_id,
        idempotency_key=idempotency_key,
        yes=yes,
        shop_dir=shop_dir,
        merchants_path=merchants_path,
    )


def run_order_create_command(
    sku: Optional[str] = None,
    quantity: int = 1,
    from_cart: bool = False,
    session_id: Optional[str] = None,
    mandate_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    yes: bool = False,
    shop_dir: Path = SHOP_DIR,
    merchants_path: Path = MERCHANTS_PATH,
) -> None:
    if not idempotency_key:
        _error("missing_idempotency_key", "--idempotency-key is required", 1)

    if not yes:
        _error("confirmation_required", "Pass --yes to confirm order creation", 1)

    if bool(sku) == bool(from_cart):
        _error("invalid_args", "Exactly one of --sku or --from-cart must be set", 1)

    asyncio.run(
        _run_order_create(
            sku=sku,
            quantity=quantity,
            from_cart=from_cart,
            session_id=session_id,
            mandate_id=mandate_id,
            idempotency_key=idempotency_key,
            shop_dir=shop_dir,
            merchants_path=merchants_path,
        )
    )


@app.command("status")
def order_status(
    order_id: str = typer.Option(..., "--order-id"),
    shop_dir: Path = SHOP_DIR,
) -> None:
    run_order_status_command(order_id=order_id, shop_dir=shop_dir)


def run_order_status_command(order_id: str, shop_dir: Path = SHOP_DIR) -> None:
    conn = get_db(shop_dir)
    row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
    conn.close()

    if not row:
        _error("order_not_found", f"Order not found: {order_id}", 4)

    tracking = {"carrier": None, "tracking_number": None, "estimated_delivery": None}
    if row["raw_response"]:
        try:
            tracking = json.loads(row["raw_response"]).get("tracking", tracking)
        except Exception:
            pass

    created_at = datetime.fromtimestamp(row["timestamp"], tz=timezone.utc).isoformat()

    _emit(
        {
            "order_id": row["order_id"],
            "status": row["status"],
            "sku": row["sku"],
            "merchant": row["merchant"],
            "price_usd": row["price_usd"],
            "mandate_id": row["mandate_id"],
            "created_at": created_at,
            "tracking": tracking,
        }
    )
