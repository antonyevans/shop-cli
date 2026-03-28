"""shop mandate — mandate create/list/verify/usage commands."""

from __future__ import annotations

import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import typer

from shop.config import SHOP_DIR
from shop.db import get_db
from shop.mandate_utils import (
    MandateNotFoundError,
    compute_period_start,
    get_or_create_device_key,
    get_period_spend,
    is_mandate_expired,
    list_mandates,
    load_mandate,
    save_mandate,
    sign_mandate,
    verify_mandate,
)

app = typer.Typer()


def _emit(data: dict, exit_code: int = 0) -> None:
    print(json.dumps(data, ensure_ascii=False))
    sys.exit(exit_code)


def _error(error_code: str, detail: str, exit_code: int) -> None:
    _emit({"error_code": error_code, "detail": detail, "exit_code": exit_code}, exit_code)


@app.command("create")
def mandate_create(
    budget_total: float = typer.Option(..., "--budget-total"),
    per_order_max: float = typer.Option(..., "--per-order-max"),
    period: str = typer.Option(..., "--period"),
    category_allow: str | None = typer.Option(None, "--category-allow"),
    category_deny: str | None = typer.Option(None, "--category-deny"),
    merchant_allow: str | None = typer.Option(None, "--merchant-allow"),
    merchant_deny: str | None = typer.Option(None, "--merchant-deny"),
    expires_at: str | None = typer.Option(None, "--expires-at"),
    shop_dir: Path = SHOP_DIR,
) -> None:
    run_mandate_create_command(
        budget_total=budget_total,
        per_order_max=per_order_max,
        period=period,
        category_allow=category_allow,
        category_deny=category_deny,
        merchant_allow=merchant_allow,
        merchant_deny=merchant_deny,
        expires_at=expires_at,
        shop_dir=shop_dir,
    )


def run_mandate_create_command(
    budget_total: float,
    per_order_max: float,
    period: str,
    category_allow: str | None,
    category_deny: str | None,
    merchant_allow: str | None,
    merchant_deny: str | None,
    expires_at: str | None,
    shop_dir: Path = SHOP_DIR,
) -> None:
    if period not in ("monthly", "weekly", "one-time"):
        _error("invalid_period", "period must be monthly, weekly, or one-time", 1)

    mandate_id = str(uuid.uuid4())
    now_iso = datetime.now(UTC).isoformat()

    def _split(s: str | None) -> list[str]:
        if not s:
            return []
        return [x.strip() for x in s.split(",") if x.strip()]

    mandate_data: dict = {
        "mandate_id": mandate_id,
        "version": 1,
        "created_at": now_iso,
        "expires_at": expires_at,
        "budget": {
            "total_usd": budget_total,
            "per_order_max_usd": per_order_max,
            "period": period,
            "period_anchor": now_iso,
        },
        "categories": {
            "allow": _split(category_allow),
            "deny": _split(category_deny),
        },
        "merchants": {
            "allow": _split(merchant_allow),
            "deny": _split(merchant_deny),
        },
    }

    keys_dir = shop_dir / "keys"
    private_key = get_or_create_device_key(keys_dir)
    sig_b64, pub_b64 = sign_mandate(mandate_data, private_key)
    mandate_data["signature"] = sig_b64
    mandate_data["public_key"] = pub_b64

    mandates_dir = shop_dir / "mandates"
    file_path = save_mandate(mandate_data, mandates_dir)

    _emit(
        {
            "mandate_id": mandate_id,
            "file_path": str(file_path.resolve()),
            "signature_valid": True,
        }
    )


@app.command("list")
def mandate_list(
    shop_dir: Path = SHOP_DIR,
) -> None:
    run_mandate_list_command(shop_dir=shop_dir)


def run_mandate_list_command(shop_dir: Path = SHOP_DIR) -> None:
    mandates_dir = shop_dir / "mandates"
    mandates = list_mandates(mandates_dir)
    conn = get_db(shop_dir)

    result = []
    for m in mandates:
        period = m.get("budget", {}).get("period", "monthly")
        period_start = compute_period_start(period)
        mandate_id = m["mandate_id"]
        spent = get_period_spend(conn, mandate_id, period_start)
        total = m.get("budget", {}).get("total_usd", 0.0)
        remaining = max(0.0, total - spent)

        if is_mandate_expired(m):
            status = "expired"
        else:
            status = "active"

        result.append(
            {
                "mandate_id": mandate_id,
                "status": status,
                "budget": {
                    "total_usd": total,
                    "spent_usd": round(spent, 2),
                    "remaining_usd": round(remaining, 2),
                    "per_order_max_usd": m.get("budget", {}).get("per_order_max_usd"),
                    "period": period,
                },
                "expires_at": m.get("expires_at"),
                "signature_valid": verify_mandate(m),
            }
        )

    conn.close()
    _emit({"mandates": result, "total": len(result)})


@app.command("verify")
def mandate_verify(
    mandate_id: str = typer.Option(..., "--mandate-id"),
    shop_dir: Path = SHOP_DIR,
) -> None:
    run_mandate_verify_command(mandate_id=mandate_id, shop_dir=shop_dir)


def run_mandate_verify_command(mandate_id: str, shop_dir: Path = SHOP_DIR) -> None:
    mandates_dir = shop_dir / "mandates"
    try:
        m = load_mandate(mandate_id, mandates_dir)
    except MandateNotFoundError:
        _error("mandate_not_found", f"Mandate not found: {mandate_id}", 4)

    valid = verify_mandate(m)
    expired = is_mandate_expired(m)

    _emit(
        {
            "mandate_id": mandate_id,
            "signature_valid": valid,
            "tamper_detected": not valid,
            "expires_at": m.get("expires_at"),
            "expired": expired,
        }
    )


@app.command("usage")
def mandate_usage(
    mandate_id: str | None = typer.Option(None, "--mandate-id"),
    shop_dir: Path = SHOP_DIR,
) -> None:
    run_mandate_usage_command(mandate_id=mandate_id, shop_dir=shop_dir)


def run_mandate_usage_command(mandate_id: str | None, shop_dir: Path = SHOP_DIR) -> None:
    if not mandate_id:
        from shop.config import load_config

        cfg = load_config(shop_dir / "config.yaml")
        mandate_id = cfg.default_mandate

    if not mandate_id:
        _error("no_mandate", "No mandate specified and no default_mandate in config", 1)

    mandates_dir = shop_dir / "mandates"
    try:
        m = load_mandate(mandate_id, mandates_dir)
    except MandateNotFoundError:
        _error("mandate_not_found", f"Mandate not found: {mandate_id}", 4)

    conn = get_db(shop_dir)
    period = m.get("budget", {}).get("period", "monthly")
    period_start = compute_period_start(period)
    spent = get_period_spend(conn, mandate_id, period_start)
    total = m.get("budget", {}).get("total_usd", 0.0)
    remaining = max(0.0, total - spent)

    # per_category_spend
    cat_rows = conn.execute(
        """
        SELECT category, SUM(amount_usd) as total
        FROM mandate_spend
        WHERE mandate_id = ? AND status IN ('pending', 'confirmed')
        GROUP BY category
        """,
        (mandate_id,),
    ).fetchall()
    per_category = [
        {"category": row["category"], "amount": round(row["total"], 2)} for row in cat_rows
    ]

    # pending_orders
    pending_rows = conn.execute(
        """
        SELECT ms.order_id, ms.amount_usd, o.merchant, ms.status, o.timestamp
        FROM mandate_spend ms
        JOIN orders o ON ms.order_id = o.order_id
        WHERE ms.mandate_id = ? AND ms.status = 'pending'
        """,
        (mandate_id,),
    ).fetchall()
    pending_orders = [
        {
            "order_id": row["order_id"],
            "amount": round(row["amount_usd"], 2),
            "merchant": row["merchant"],
            "status": row["status"],
            "created_at": datetime.fromtimestamp(row["timestamp"], tz=UTC).isoformat(),
        }
        for row in pending_rows
    ]

    conn.close()
    _emit(
        {
            "mandate_id": mandate_id,
            "budget_total": total,
            "budget_used": round(spent, 2),
            "budget_remaining": round(remaining, 2),
            "per_category_spend": per_category,
            "pending_orders": pending_orders,
        }
    )
