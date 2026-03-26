"""shop payment — register payment methods via Stripe's hosted card setup flow.

Card details are entered directly in Stripe's PCI-compliant hosted page.
The CLI and agent never see raw card numbers.

Credentials stored in ~/.shop/payment.yaml (chmod 600):
  - type: stripe
  - customer_id / payment_method_id (opaque Stripe IDs)
  - card_last4, card_brand, expiry (display metadata only)

Stripe key resolution: --stripe-key flag > STRIPE_SECRET_KEY env var.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx
import typer
import yaml

from shop.config import SHOP_DIR

app = typer.Typer()

_STRIPE_API = "https://api.stripe.com/v1"
_POLL_INTERVAL = 3  # seconds between status checks
_SETUP_EXPIRES_IN = 3600  # report 1-hour window to users


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
        return {"default": None, "methods": [], "pending": []}
    with p.open() as f:
        data = yaml.safe_load(f) or {}
    data.setdefault("default", None)
    data.setdefault("methods", [])
    data.setdefault("pending", [])
    return data


def _save_payment_file(data: dict, shop_dir: Path) -> None:
    p = _payment_path(shop_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
    p.chmod(0o600)


def _stripe_get(stripe_key: str, path: str) -> dict:
    r = httpx.get(f"{_STRIPE_API}{path}", auth=(stripe_key, ""), timeout=10.0)
    r.raise_for_status()
    return r.json()


def _stripe_post(stripe_key: str, path: str, data: dict) -> dict:
    r = httpx.post(f"{_STRIPE_API}{path}", data=data, auth=(stripe_key, ""), timeout=10.0)
    r.raise_for_status()
    return r.json()


def _stripe_error_message(exc: httpx.HTTPStatusError) -> str:
    try:
        return exc.response.json().get("error", {}).get("message", str(exc))
    except Exception:
        return str(exc)


@app.command("add")
def payment_add(
    label: str = typer.Option(..., "--label", help="Name for this payment method"),
    email: str = typer.Option("", "--email", help="Customer email (optional, for Stripe records)"),
    stripe_key: str = typer.Option(
        "", "--stripe-key", envvar="STRIPE_SECRET_KEY",
        help="Stripe secret key (or set STRIPE_SECRET_KEY env var)",
    ),
    shop_dir: Path = SHOP_DIR,
) -> None:
    """Start secure card setup via Stripe. Returns a browser URL for card entry.

    The agent never sees your card number — you enter it directly in Stripe's
    PCI-compliant hosted page. Run `shop payment confirm` after completing the form.
    """
    run_payment_add_command(label=label, email=email, stripe_key=stripe_key, shop_dir=shop_dir)


def run_payment_add_command(
    label: str,
    email: str = "",
    stripe_key: str = "",
    shop_dir: Path = SHOP_DIR,
) -> None:
    if not stripe_key:
        _error(
            "missing_stripe_key",
            "Stripe secret key required. Set STRIPE_SECRET_KEY or pass --stripe-key.",
            1,
        )

    try:
        # Create Stripe Customer
        customer_body: dict = {"description": f"shop-cli: {label}"}
        if email:
            customer_body["email"] = email
        customer = _stripe_post(stripe_key, "/customers", customer_body)
        customer_id: str = customer["id"]

        # Create Stripe Checkout Session (mode=setup)
        session = _stripe_post(stripe_key, "/checkout/sessions", {
            "mode": "setup",
            "customer": customer_id,
            "payment_method_types[]": "card",
            # Placeholder URLs — users open this in their browser, not a real redirect target
            "success_url": "https://shop-cli.dev/payment/success?session_id={CHECKOUT_SESSION_ID}",
            "cancel_url": "https://shop-cli.dev/payment/cancel",
        })
        session_id: str = session["id"]
        setup_url: str = session["url"]

    except httpx.HTTPStatusError as e:
        _error("stripe_error", _stripe_error_message(e), 2)
    except Exception as e:
        _error("stripe_error", str(e), 6)

    # Persist pending entry so `payment confirm` can resolve label + customer_id
    data = _load_payment_file(shop_dir)
    data["pending"] = [p for p in data["pending"] if p.get("label") != label]
    data["pending"].append({
        "label": label,
        "session_id": session_id,
        "customer_id": customer_id,
        "email": email,
    })
    _save_payment_file(data, shop_dir)

    _emit({
        "status": "pending",
        "label": label,
        "session_id": session_id,
        "setup_url": setup_url,
        "expires_in": _SETUP_EXPIRES_IN,
        "next_step": f"Open the URL in a browser, enter your card, then run: "
                     f"shop payment confirm --session-id {session_id}",
    })


@app.command("confirm")
def payment_confirm(
    session_id: str = typer.Option(..., "--session-id", help="Stripe checkout session ID from `payment add`"),
    timeout: int = typer.Option(300, "--timeout", help="Max seconds to wait for completion"),
    stripe_key: str = typer.Option(
        "", "--stripe-key", envvar="STRIPE_SECRET_KEY",
        help="Stripe secret key (or set STRIPE_SECRET_KEY env var)",
    ),
    shop_dir: Path = SHOP_DIR,
) -> None:
    """Poll Stripe until card setup is complete, then store credentials locally."""
    run_payment_confirm_command(
        session_id=session_id, timeout=timeout, stripe_key=stripe_key, shop_dir=shop_dir,
    )


def run_payment_confirm_command(
    session_id: str,
    timeout: int = 300,
    stripe_key: str = "",
    shop_dir: Path = SHOP_DIR,
) -> None:
    if not stripe_key:
        _error(
            "missing_stripe_key",
            "Stripe secret key required. Set STRIPE_SECRET_KEY or pass --stripe-key.",
            1,
        )

    data = _load_payment_file(shop_dir)
    pending_entry = next(
        (p for p in data["pending"] if p["session_id"] == session_id), None
    )

    # Poll until session is complete
    deadline = time.monotonic() + timeout
    session_data: dict = {}
    while time.monotonic() < deadline:
        try:
            session_data = _stripe_get(stripe_key, f"/checkout/sessions/{session_id}")
        except httpx.HTTPStatusError as e:
            _error("stripe_error", _stripe_error_message(e), 2)
        except Exception as e:
            _error("stripe_error", str(e), 6)

        status = session_data.get("status", "")
        if status == "complete":
            break
        if status == "expired":
            _error(
                "session_expired",
                "Stripe checkout session expired. Run `shop payment add` again.",
                1,
            )
        time.sleep(_POLL_INTERVAL)
    else:
        _error(
            "timeout",
            f"Card setup not completed within {timeout}s. Run `shop payment confirm` again once done.",
            6,
        )

    # Retrieve payment method details from SetupIntent
    setup_intent_id = session_data.get("setup_intent")
    customer_id = session_data.get("customer") or (pending_entry or {}).get("customer_id")
    label = (pending_entry or {}).get("label") or session_id

    try:
        si = _stripe_get(stripe_key, f"/setup_intents/{setup_intent_id}")
        pm_id: str = si["payment_method"]
        pm = _stripe_get(stripe_key, f"/payment_methods/{pm_id}")
    except httpx.HTTPStatusError as e:
        _error("stripe_error", _stripe_error_message(e), 2)
    except Exception as e:
        _error("stripe_error", f"Could not retrieve payment method details: {e}", 2)

    card_info = pm.get("card", {})
    card_last4: str = card_info.get("last4", "????")
    card_brand: str = card_info.get("brand", "card")
    exp_month: int = card_info.get("exp_month", 0)
    exp_year: int = card_info.get("exp_year", 0)
    expiry = f"{exp_month}/{exp_year}"

    method = {
        "id": pm_id,
        "label": label,
        "type": "stripe",
        "customer_id": customer_id,
        "payment_method_id": pm_id,
        "card_last4": card_last4,
        "card_brand": card_brand,
        "expiry": expiry,
    }

    # Move from pending → methods
    data["pending"] = [p for p in data["pending"] if p["session_id"] != session_id]
    data["methods"] = [m for m in data["methods"] if m.get("label") != label]
    data["methods"].append(method)
    if not data.get("default"):
        data["default"] = pm_id

    _save_payment_file(data, shop_dir)
    _emit({
        "status": "confirmed",
        "method_id": pm_id,
        "label": label,
        "card_last4": card_last4,
        "card_brand": card_brand,
        "expiry": expiry,
        "default": data["default"] == pm_id,
    })


@app.command("list")
def payment_list(shop_dir: Path = SHOP_DIR) -> None:
    """List stored payment methods (no sensitive data exposed)."""
    run_payment_list_command(shop_dir=shop_dir)


def run_payment_list_command(shop_dir: Path = SHOP_DIR) -> None:
    data = _load_payment_file(shop_dir)
    methods = []
    for m in data.get("methods", []):
        methods.append({
            "id": m["id"],
            "label": m.get("label", ""),
            "type": m.get("type", "stripe"),
            "card_last4": m.get("card_last4", "????"),
            "card_brand": m.get("card_brand", ""),
            "expiry": m.get("expiry", ""),
            "default": m["id"] == data.get("default"),
        })
    pending_count = len(data.get("pending", []))
    _emit({"methods": methods, "count": len(methods), "pending_setups": pending_count})
