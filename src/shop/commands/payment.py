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
        "",
        "--stripe-key",
        envvar="STRIPE_SECRET_KEY",
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
        session = _stripe_post(
            stripe_key,
            "/checkout/sessions",
            {
                "mode": "setup",
                "customer": customer_id,
                "payment_method_types[]": "card",
                # Placeholder URLs — users open this in their browser, not a real redirect target
                "success_url": "https://shop-cli.dev/payment/success?session_id={CHECKOUT_SESSION_ID}",
                "cancel_url": "https://shop-cli.dev/payment/cancel",
            },
        )
        session_id: str = session["id"]
        setup_url: str = session["url"]

    except httpx.HTTPStatusError as e:
        _error("stripe_error", _stripe_error_message(e), 2)
    except Exception as e:
        _error("stripe_error", str(e), 6)

    # Persist pending entry so `payment confirm` can resolve label + customer_id
    data = _load_payment_file(shop_dir)
    data["pending"] = [p for p in data["pending"] if p.get("label") != label]
    data["pending"].append(
        {
            "label": label,
            "session_id": session_id,
            "customer_id": customer_id,
            "email": email,
        }
    )
    _save_payment_file(data, shop_dir)

    _emit(
        {
            "status": "pending",
            "label": label,
            "session_id": session_id,
            "setup_url": setup_url,
            "expires_in": _SETUP_EXPIRES_IN,
            "next_step": f"Open the URL in a browser, enter your card, then run: "
            f"shop payment confirm --session-id {session_id}",
        }
    )


@app.command("confirm")
def payment_confirm(
    session_id: str = typer.Option(
        ..., "--session-id", help="Stripe checkout session ID from `payment add`"
    ),
    timeout: int = typer.Option(300, "--timeout", help="Max seconds to wait for completion"),
    stripe_key: str = typer.Option(
        "",
        "--stripe-key",
        envvar="STRIPE_SECRET_KEY",
        help="Stripe secret key (or set STRIPE_SECRET_KEY env var)",
    ),
    shop_dir: Path = SHOP_DIR,
) -> None:
    """Poll Stripe until card setup is complete, then store credentials locally."""
    run_payment_confirm_command(
        session_id=session_id,
        timeout=timeout,
        stripe_key=stripe_key,
        shop_dir=shop_dir,
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
    pending_entry = next((p for p in data["pending"] if p["session_id"] == session_id), None)

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
            f"Card setup not completed within {timeout}s."
            " Run `shop payment confirm` again once done.",
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
    _emit(
        {
            "status": "confirmed",
            "method_id": pm_id,
            "label": label,
            "card_last4": card_last4,
            "card_brand": card_brand,
            "expiry": expiry,
            "default": data["default"] == pm_id,
        }
    )


@app.command("add-shop-pay")
def payment_add_shop_pay(
    label: str = typer.Option("Shop Pay", "--label", help="Name for this payment method"),
    token: str = typer.Option(..., "--token", help="Shop Pay token from Shop Pay authorization"),
    email: str = typer.Option(..., "--email", help="Buyer email registered with Shop Pay"),
    first_name: str = typer.Option("", "--first-name"),
    last_name: str = typer.Option("", "--last-name"),
    address1: str = typer.Option("", "--address1"),
    city: str = typer.Option("", "--city"),
    province: str = typer.Option("", "--province", help="State/province code"),
    country: str = typer.Option("US", "--country", help="ISO 3166-1 alpha-2"),
    zip_code: str = typer.Option("", "--zip"),
    shop_dir: Path = SHOP_DIR,
) -> None:
    """Store a Shop Pay token for agent checkout via Shopify's UCP/MCP protocol.

    The Shop Pay token is obtained by authorizing the agent with Shop Pay.
    Card details are stored securely by Shop Pay — the agent only uses the token.
    """
    run_payment_add_shop_pay_command(
        label=label,
        token=token,
        email=email,
        first_name=first_name,
        last_name=last_name,
        address1=address1,
        city=city,
        province=province,
        country=country,
        zip_code=zip_code,
        shop_dir=shop_dir,
    )


def run_payment_add_shop_pay_command(
    label: str,
    token: str,
    email: str,
    first_name: str = "",
    last_name: str = "",
    address1: str = "",
    city: str = "",
    province: str = "",
    country: str = "US",
    zip_code: str = "",
    shop_dir: Path = SHOP_DIR,
) -> None:
    import uuid

    if not token:
        _error("missing_token", "Shop Pay token required (--token)", 1)
    if not email:
        _error("missing_email", "Buyer email required (--email)", 1)

    method_id = f"shoppay_{uuid.uuid4().hex[:8]}"
    billing_address = {
        "first_name": first_name,
        "last_name": last_name,
        "street_address": address1,
        "address_locality": city,
        "address_region": province,
        "postal_code": zip_code,
        "address_country": country,
    }

    method = {
        "id": method_id,
        "label": label,
        "type": "shop_pay",
        "email": email,
        "shop_pay_token": token,
        "billing_address": billing_address,
    }

    data = _load_payment_file(shop_dir)
    data["methods"] = [m for m in data["methods"] if m.get("label") != label]
    data["methods"].append(method)
    if not data.get("default"):
        data["default"] = method_id

    _save_payment_file(data, shop_dir)
    _emit(
        {
            "status": "added",
            "method_id": method_id,
            "label": label,
            "type": "shop_pay",
            "email": email,
            "default": data["default"] == method_id,
        }
    )


@app.command("add-paypal-fastlane")
def payment_add_paypal_fastlane(
    label: str = typer.Option("PayPal Fastlane", "--label", help="Name for this payment method"),
    token: str = typer.Option(
        ..., "--token", help="Fastlane payment token from PayPal authorization"
    ),
    email: str = typer.Option("", "--email", help="Buyer email registered with PayPal"),
    name: str = typer.Option("", "--name", help="Buyer full name (for order records)"),
    address1: str = typer.Option("", "--address1"),
    city: str = typer.Option("", "--city"),
    province: str = typer.Option("", "--province", help="State/province code"),
    country: str = typer.Option("US", "--country", help="ISO 3166-1 alpha-2"),
    zip_code: str = typer.Option("", "--zip"),
    shop_dir: Path = SHOP_DIR,
) -> None:
    """Store a PayPal Fastlane token for agent checkout at PayPal-enabled merchants.

    Fastlane is PayPal's headless checkout for returning customers. The token
    authorizes purchases without exposing card details to the agent.
    """
    run_payment_add_paypal_fastlane_command(
        label=label,
        token=token,
        email=email,
        name=name,
        address1=address1,
        city=city,
        province=province,
        country=country,
        zip_code=zip_code,
        shop_dir=shop_dir,
    )


def run_payment_add_paypal_fastlane_command(
    label: str,
    token: str,
    email: str = "",
    name: str = "",
    address1: str = "",
    city: str = "",
    province: str = "",
    country: str = "US",
    zip_code: str = "",
    shop_dir: Path = SHOP_DIR,
) -> None:
    import uuid as _uuid

    if not token:
        _error("missing_token", "PayPal Fastlane token required (--token)", 1)

    method_id = f"ppfl_{_uuid.uuid4().hex[:8]}"
    billing_address = {
        "name": name,
        "address1": address1,
        "city": city,
        "province": province,
        "zip": zip_code,
        "country": country,
    }

    method = {
        "id": method_id,
        "label": label,
        "type": "paypal_fastlane",
        "email": email,
        "name": name,
        "fastlane_token": token,
        "billing_address": billing_address,
    }

    data = _load_payment_file(shop_dir)
    data["methods"] = [m for m in data["methods"] if m.get("label") != label]
    data["methods"].append(method)
    if not data.get("default"):
        data["default"] = method_id

    _save_payment_file(data, shop_dir)
    _emit(
        {
            "status": "added",
            "method_id": method_id,
            "label": label,
            "type": "paypal_fastlane",
            "email": email,
            "default": data["default"] == method_id,
        }
    )


@app.command("add-bolt")
def payment_add_bolt(
    label: str = typer.Option("Bolt", "--label", help="Name for this payment method"),
    token: str = typer.Option(
        ..., "--token", help="Bolt payment token from Bolt account authorization"
    ),
    email: str = typer.Option("", "--email", help="Buyer email registered with Bolt"),
    name: str = typer.Option("", "--name", help="Buyer full name"),
    address1: str = typer.Option("", "--address1"),
    city: str = typer.Option("", "--city"),
    province: str = typer.Option("", "--province", help="State/province code"),
    country: str = typer.Option("US", "--country", help="ISO 3166-1 alpha-2"),
    zip_code: str = typer.Option("", "--zip"),
    shop_dir: Path = SHOP_DIR,
) -> None:
    """Store a Bolt payment token for agent checkout at Bolt-enabled merchants.

    Bolt's universal checkout network allows one-click purchasing across all
    Bolt-integrated merchants. The token is obtained by authorizing with Bolt.
    """
    run_payment_add_bolt_command(
        label=label,
        token=token,
        email=email,
        name=name,
        address1=address1,
        city=city,
        province=province,
        country=country,
        zip_code=zip_code,
        shop_dir=shop_dir,
    )


def run_payment_add_bolt_command(
    label: str,
    token: str,
    email: str = "",
    name: str = "",
    address1: str = "",
    city: str = "",
    province: str = "",
    country: str = "US",
    zip_code: str = "",
    shop_dir: Path = SHOP_DIR,
) -> None:
    import uuid as _uuid

    if not token:
        _error("missing_token", "Bolt payment token required (--token)", 1)

    method_id = f"bolt_{_uuid.uuid4().hex[:8]}"
    billing_address = {
        "name": name,
        "street_address1": address1,
        "locality": city,
        "region": province,
        "postal_code": zip_code,
        "country_code": country,
    }

    method = {
        "id": method_id,
        "label": label,
        "type": "bolt",
        "email": email,
        "name": name,
        "bolt_token": token,
        "billing_address": billing_address,
    }

    data = _load_payment_file(shop_dir)
    data["methods"] = [m for m in data["methods"] if m.get("label") != label]
    data["methods"].append(method)
    if not data.get("default"):
        data["default"] = method_id

    _save_payment_file(data, shop_dir)
    _emit(
        {
            "status": "added",
            "method_id": method_id,
            "label": label,
            "type": "bolt",
            "email": email,
            "default": data["default"] == method_id,
        }
    )


@app.command("add-card")
def payment_add_card(
    label: str = typer.Option(..., "--label", help="Name for this payment method"),
    number: str = typer.Option(..., "--number", help="Card number (dev/test use only)"),
    first_name: str = typer.Option(..., "--first-name"),
    last_name: str = typer.Option(..., "--last-name"),
    month: int = typer.Option(..., "--month", help="Expiry month (1-12)"),
    year: int = typer.Option(..., "--year", help="Expiry year"),
    cvv: str = typer.Option(..., "--cvv"),
    address1: str = typer.Option("", "--address1"),
    city: str = typer.Option("", "--city"),
    province: str = typer.Option("", "--province"),
    country: str = typer.Option("US", "--country"),
    zip_code: str = typer.Option("", "--zip"),
    shop_dir: Path = SHOP_DIR,
) -> None:
    """[DEV/TEST ONLY] Store raw card credentials for Shopify headless checkout.

    WARNING: Raw card details are stored in ~/.shop/payment.yaml. Use only
    with test card numbers (e.g. Stripe 4242...) against dev/test stores.
    Production use requires Stripe Setup Intent flow (shop payment add).
    """
    run_payment_add_card_command(
        label=label,
        number=number,
        first_name=first_name,
        last_name=last_name,
        month=month,
        year=year,
        cvv=cvv,
        address1=address1,
        city=city,
        province=province,
        country=country,
        zip_code=zip_code,
        shop_dir=shop_dir,
    )


def run_payment_add_card_command(
    label: str,
    number: str,
    first_name: str,
    last_name: str,
    month: int,
    year: int,
    cvv: str,
    address1: str = "",
    city: str = "",
    province: str = "",
    country: str = "US",
    zip_code: str = "",
    shop_dir: Path = SHOP_DIR,
) -> None:
    import re
    import uuid

    clean_number = number.replace(" ", "").replace("-", "")
    if not re.fullmatch(r"\d{12,19}", clean_number):
        _error("invalid_card", "Card number must be 12-19 digits", 1)
    if not 1 <= month <= 12:
        _error("invalid_card", "Expiry month must be 1-12", 1)

    method_id = f"card_{uuid.uuid4().hex[:8]}"
    billing = {
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
        "number": clean_number,
        "first_name": first_name,
        "last_name": last_name,
        "month": month,
        "year": year,
        "cvv": cvv,
        "billing": billing,
        "card_last4": clean_number[-4:],
        "card_brand": "card",
        "expiry": f"{month}/{year}",
        "_dev_only": True,
    }

    data = _load_payment_file(shop_dir)
    data["methods"] = [m for m in data["methods"] if m.get("label") != label]
    data["methods"].append(method)
    if not data.get("default"):
        data["default"] = method_id

    _save_payment_file(data, shop_dir)
    _emit(
        {
            "status": "added",
            "method_id": method_id,
            "label": label,
            "card_last4": clean_number[-4:],
            "type": "credit_card",
            "warning": "DEV/TEST ONLY — raw card stored. Use shop payment add for production.",
            "default": data["default"] == method_id,
        }
    )


@app.command("remove")
def payment_remove(
    method_id: str = typer.Option(..., "--id", help="Payment method ID to remove"),
    shop_dir: Path = SHOP_DIR,
) -> None:
    """Remove a stored payment method."""
    run_payment_remove_command(method_id=method_id, shop_dir=shop_dir)


def run_payment_remove_command(method_id: str, shop_dir: Path = SHOP_DIR) -> None:
    data = _load_payment_file(shop_dir)
    before = len(data["methods"])
    data["methods"] = [m for m in data["methods"] if m["id"] != method_id]

    if len(data["methods"]) == before:
        _error("not_found", f"No payment method with id '{method_id}'", 1)

    # Reset default if the removed method was the default
    if data.get("default") == method_id:
        data["default"] = data["methods"][0]["id"] if data["methods"] else None

    _save_payment_file(data, shop_dir)
    _emit({"status": "removed", "method_id": method_id, "remaining": len(data["methods"])})


@app.command("list")
def payment_list(shop_dir: Path = SHOP_DIR) -> None:
    """List stored payment methods (no sensitive data exposed)."""
    run_payment_list_command(shop_dir=shop_dir)


def run_payment_list_command(shop_dir: Path = SHOP_DIR) -> None:
    data = _load_payment_file(shop_dir)
    methods = []
    for m in data.get("methods", []):
        methods.append(
            {
                "id": m["id"],
                "label": m.get("label", ""),
                "type": m.get("type", "stripe"),
                "card_last4": m.get("card_last4", "????"),
                "card_brand": m.get("card_brand", ""),
                "expiry": m.get("expiry", ""),
                "default": m["id"] == data.get("default"),
            }
        )
    pending_count = len(data.get("pending", []))
    _emit({"methods": methods, "count": len(methods), "pending_setups": pending_count})
