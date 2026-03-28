"""BoltAdapter — checkout via Bolt's universal checkout network.

Bolt is a universal checkout platform built for headless commerce. Merchants
integrate Bolt once; buyers authenticate via Bolt's identity network and can
check out at any Bolt merchant using a stored payment + shipping profile.

Protocol:  REST/JSON (Bolt Commerce API)
Auth:      OAuth 2.0 — merchant API key → request signing
Payment:   Bolt payment token from ~/.shop/payment.yaml (type: bolt)
API base:  https://api.bolt.com (live) / https://api.bolt.com (sandbox with test creds)

Checkout flow:
  1. POST /v1/guest/checkout or /v1/account/checkout — create checkout session
  2. Checkout response includes order_reference on success

Config keys (from merchants.yaml extra fields):
  bolt_api_key       — merchant's Bolt API key (publishable key for checkout)
  bolt_merchant_id   — merchant's Bolt merchant ID
  bolt_sandbox       — "true" to use sandbox mode (default: false)
  currency           — ISO 4217 currency code (default: USD)
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Optional

import httpx
import yaml

from shop.adapters.base import (
    AdapterError,
    CheckoutNotSupportedError,
    MerchantAdapter,
    ProductNotFoundError,
)
from shop.models.commerce import CommerceTXTProduct, SearchFilters

_API_BASE = "https://api.bolt.com"
_SANDBOX_API_BASE = "https://api-sandbox.bolt.com"
_TIMEOUT = 10.0


def _get_shop_dir() -> Path:
    return Path(os.environ["SHOP_HOME"]) if "SHOP_HOME" in os.environ else Path.home() / ".shop"


def _load_bolt_credential(shop_dir: Path) -> Optional[dict]:
    """Load Bolt payment credential from payment.yaml.

    Returns {token, email, name, billing_address} or None.
    """
    payment_path = shop_dir / "payment.yaml"
    if not payment_path.exists():
        return None
    try:
        with payment_path.open() as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return None

    methods = data.get("methods", [])
    if not methods:
        return None

    default_id = data.get("default")
    bolt_methods = [m for m in methods if m.get("type") == "bolt"]
    method = next((m for m in bolt_methods if m["id"] == default_id), None) or (
        bolt_methods[0] if bolt_methods else None
    )
    if not method:
        return None

    token = method.get("bolt_token")
    if not token:
        return None

    return {
        "token": token,
        "email": method.get("email", ""),
        "name": method.get("name", ""),
        "billing_address": method.get("billing_address", {}),
    }


class BoltAdapter(MerchantAdapter):
    """Headless checkout via Bolt's universal checkout network.

    Uses the merchant's Bolt API key to authenticate checkout requests.
    Payment is authorized via the buyer's stored Bolt token — no raw card
    details are exposed to the agent.

    Requires `bolt_api_key` + `bolt_merchant_id` in merchant config,
    and a `bolt` credential in ~/.shop/payment.yaml.
    Run `shop payment add-bolt` to store a Bolt token.
    """

    def __init__(self, slug: str, config: dict) -> None:
        super().__init__(slug, config)
        self.api_key = config.get("bolt_api_key", "")
        self.merchant_id = config.get("bolt_merchant_id", "")
        self.sandbox = str(config.get("bolt_sandbox", "")).lower() == "true"
        self.currency = config.get("currency", "USD").upper()
        self._base = _SANDBOX_API_BASE if self.sandbox else _API_BASE

    def _headers(self, idempotency_key: str) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": f"api-key {self.api_key}",
            "X-Bolt-Merchant-Id": self.merchant_id,
            "X-Request-Id": str(uuid.uuid4()),
            "Idempotency-Key": idempotency_key,
        }

    async def search(self, query: str, filters: SearchFilters) -> list[CommerceTXTProduct]:
        raise AdapterError(
            self.slug,
            "BoltAdapter handles checkout only. Use ShopifyCatalogAdapter for search.",
        )

    async def get_product(self, sku: str) -> CommerceTXTProduct:
        raise ProductNotFoundError(
            self.slug,
            "BoltAdapter does not expose product detail.",
        )

    async def get_capabilities(self) -> dict:
        return {
            "search": False,
            "order_create": True,
            "product_detail": False,
            "adapter": "bolt",
            "sandbox": self.sandbox,
            "currency": self.currency,
            "payment_handler": "bolt",
        }

    async def create_order(
        self,
        sku: str,
        quantity: int,
        mandate_id: str,
        idempotency_key: str,
        checkout_url: Optional[str] = None,
    ) -> dict:
        """Place a headless order via Bolt's checkout API.

        Single-phase: POST /v1/account/checkout
        Returns order_reference on success.
        """
        if not self.api_key or not self.merchant_id:
            raise AdapterError(
                self.slug, "bolt_api_key and bolt_merchant_id required in merchant config"
            )

        cred = _load_bolt_credential(_get_shop_dir())
        if not cred:
            raise AdapterError(
                self.slug,
                "No Bolt payment token configured. Run: shop payment add-bolt",
            )

        raw_sku = sku.removeprefix(f"{self.slug}:")
        billing = cred.get("billing_address", {})

        payload: dict = {
            "cart": {
                "order_reference": idempotency_key,
                "items": [
                    {
                        "reference": raw_sku,
                        "name": raw_sku,
                        "quantity": quantity,
                        "unit_price": 0,  # merchant-side pricing; 0 for agent-driven flow
                        "total_amount": 0,
                    }
                ],
                "currency": self.currency,
            },
            "payment": {
                "token": cred["token"],
                "type": "credit_card",  # Bolt abstracts card type via token
            },
        }
        if cred.get("email"):
            payload["email"] = cred["email"]
        if billing:
            payload["billing_address"] = billing

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                r = await client.post(
                    f"{self._base}/v1/account/checkout",
                    json=payload,
                    headers=self._headers(idempotency_key),
                )
                if r.status_code == 422:
                    err = r.json()
                    errors = err.get("errors", [{}])
                    msg = errors[0].get("message", r.text) if errors else r.text
                    raise AdapterError(self.slug, f"Bolt rejected checkout: {msg}")
                if r.status_code in (401, 403):
                    raise AdapterError(
                        self.slug,
                        f"Bolt auth error: HTTP {r.status_code} — check bolt_api_key",
                    )
                if r.status_code in (501, 503):
                    raise CheckoutNotSupportedError(
                        self.slug, "Bolt checkout not available for this merchant"
                    )
                r.raise_for_status()
                result = r.json()
        except (AdapterError, CheckoutNotSupportedError):
            raise
        except httpx.TimeoutException:
            raise TimeoutError()
        except httpx.HTTPStatusError as e:
            raise AdapterError(self.slug, f"Bolt HTTP {e.response.status_code}")
        except Exception as e:
            raise AdapterError(self.slug, str(e))

        # Bolt returns {transaction: {reference, status, ...}}
        transaction = result.get("transaction") or result
        order_reference = transaction.get("reference") or transaction.get("order_reference")
        if not order_reference:
            raise AdapterError(self.slug, f"No order reference in Bolt response: {result}")

        status = transaction.get("status", "completed").lower()
        if status in ("failed", "rejected", "voided"):
            raise AdapterError(
                self.slug,
                f"Bolt transaction {status}: {transaction.get('message', '')}",
            )
        if status in ("pending_review", "on_hold"):
            raise CheckoutNotSupportedError(
                self.slug,
                f"Bolt order requires review (status={status}) — human action needed",
            )

        return {
            "bolt_order_reference": order_reference,
            "bolt_transaction_id": transaction.get("id", ""),
            "status": "completed",
            "currency": self.currency,
            "adapter": "bolt",
            "sandbox": self.sandbox,
        }
