"""shop payment — store and manage payment methods for headless checkout.

Payment credentials are stored in ~/.shop/payment.yaml (chmod 600).
Card numbers are stored in plaintext — for test/development use only.
Production use should configure a payment vault (v1).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import typer
import yaml

from shop.config import SHOP_DIR

app = typer.Typer()


def _emit(data: dict, exit_code: int = 0) -> None:
    print(json.dumps(data, ensure_ascii=False))
    sys.exit(exit_code)


def _error(error_code: str, detail: str, exit_code: int) -> None:
    _emit({"error_code": error_code, "detail": detail, "exit_code": exit_code}, exit_code)


def _payment_path(shop_dir: Path) -> Path:
    return shop_dir / "payment.yaml"


def _load_payment_file(shop_dir: Path) -> dict:
    p = _payment_path(shop_dir)
    if not p.exists():
        return {"default": None, "methods": []}
    with p.open() as f:
        return yaml.safe_load(f) or {"default": None, "methods": []}


def _save_payment_file(data: dict, shop_dir: Path) -> None:
    p = _payment_path(shop_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
    p.chmod(0o600)


@app.command("add")
def payment_add(
    label: str = typer.Option(..., "--label", help="Name for this payment method"),
    number: str = typer.Option(..., "--number", help="Card number"),
    first_name: str = typer.Option(..., "--first-name"),
    last_name: str = typer.Option(..., "--last-name"),
    month: int = typer.Option(..., "--month", help="Expiry month (1-12)"),
    year: int = typer.Option(..., "--year", help="Expiry year (e.g. 2026)"),
    cvv: str = typer.Option(..., "--cvv"),
    email: str = typer.Option("agent@shop-cli.dev", "--email", help="Email for checkout"),
    address1: str = typer.Option("", "--address1"),
    city: str = typer.Option("", "--city"),
    province: str = typer.Option("", "--province", help="State/province code"),
    country: str = typer.Option("US", "--country", help="ISO 3166-1 alpha-2"),
    zip_code: str = typer.Option("", "--zip"),
    shop_dir: Path = SHOP_DIR,
) -> None:
    """Store a payment method for headless checkout. Saved to ~/.shop/payment.yaml (chmod 600)."""
    run_payment_add_command(
        label=label, number=number, first_name=first_name, last_name=last_name,
        month=month, year=year, cvv=cvv, email=email,
        address1=address1, city=city, province=province, country=country, zip_code=zip_code,
        shop_dir=shop_dir,
    )


def run_payment_add_command(
    label: str,
    number: str,
    first_name: str,
    last_name: str,
    month: int,
    year: int,
    cvv: str,
    email: str = "agent@shop-cli.dev",
    address1: str = "",
    city: str = "",
    province: str = "",
    country: str = "US",
    zip_code: str = "",
    shop_dir: Path = SHOP_DIR,
) -> None:
    import re
    import uuid

    # Basic validation
    if not re.fullmatch(r"\d{12,19}", number.replace(" ", "").replace("-", "")):
        _error("invalid_card", "Card number must be 12-19 digits", 1)
    if not 1 <= month <= 12:
        _error("invalid_card", "Expiry month must be 1-12", 1)
    if year < 2024:
        _error("invalid_card", "Expiry year must be 2024 or later", 1)

    method_id = f"card_{uuid.uuid4().hex[:6]}"
    address = {
        "address1": address1,
        "city": city,
        "province": province,
        "country": country,
        "zip": zip_code,
    }

    method = {
        "id": method_id,
        "label": label,
        "type": "credit_card",
        "number": number.replace(" ", "").replace("-", ""),
        "first_name": first_name,
        "last_name": last_name,
        "month": month,
        "year": year,
        "cvv": cvv,
        "email": email,
        "billing": address,
        "shipping": address,
    }

    data = _load_payment_file(shop_dir)
    # Replace existing method with same label
    data["methods"] = [m for m in data["methods"] if m.get("label") != label]
    data["methods"].append(method)
    if not data.get("default"):
        data["default"] = method_id

    _save_payment_file(data, shop_dir)
    _emit({
        "status": "added",
        "method_id": method_id,
        "label": label,
        "card_last4": number[-4:],
        "default": data["default"] == method_id,
    })


@app.command("list")
def payment_list(shop_dir: Path = SHOP_DIR) -> None:
    """List stored payment methods (card numbers masked)."""
    run_payment_list_command(shop_dir=shop_dir)


def run_payment_list_command(shop_dir: Path = SHOP_DIR) -> None:
    data = _load_payment_file(shop_dir)
    methods = []
    for m in data.get("methods", []):
        num = str(m.get("number", ""))
        methods.append({
            "id": m["id"],
            "label": m.get("label", ""),
            "type": m.get("type", "credit_card"),
            "card_last4": num[-4:] if len(num) >= 4 else "****",
            "expiry": f"{m.get('month', '?')}/{m.get('year', '?')}",
            "default": m["id"] == data.get("default"),
        })
    _emit({"methods": methods, "count": len(methods)})
