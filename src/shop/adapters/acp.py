"""ACPAdapter — checkout via Stripe's Agentic Commerce Protocol (ACP).

ACP is a lightweight REST checkout standard for AI agents, developed by
Stripe and OpenAI. Merchants implement a single POST endpoint; agents
pay using stored Stripe credentials (customer_id + payment_method_id).

Protocol:  REST/JSON
Discovery: GET /.well-known/acp → {version, name, acp: {endpoint, payment_handlers}}
Checkout:  POST {acp_endpoint}/checkout
Auth:      Bearer {acp_key} header (merchant-issued API key)
Payment:   Stripe customer_id + payment_method_id from ~/.shop/payment.yaml

Checkout request:
  {
    "idempotency_key": "...",
    "items": [{"sku": "...", "quantity": 1}],
    "buyer": {"email": "..."},
    "mandate_id": "...",
    "payment": {"type": "stripe", "customer_id": "cus_xxx", "payment_method_id": "pm_xxx"}
  }

Checkout response:
  {
    "order_id": "ord_xxx",
    "status": "confirmed",
    "total_cents": 1999,
    "currency": "USD"
  }

Config keys (from merchants.yaml extra fields):
  acp_endpoint  — e.g. https://merchant.com/api/acp
  acp_key       — merchant-issued API key for Bearer auth
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import httpx
import yaml

from shop.adapters.base import (
    AdapterError,
    CheckoutNotSupportedError,
    MerchantAdapter,
    ProductNotFoundError,
)
from shop.models.commerce import CommerceTXTProduct, SearchFilters

_TIMEOUT = 10.0


def _get_shop_dir() -> Path:
    return Path(os.environ["SHOP_HOME"]) if "SHOP_HOME" in os.environ else Path.home() / ".shop"


def _load_stripe_credential(shop_dir: Path) -> dict | None:
    """Load Stripe payment credentials from payment.yaml.

    Returns {"customer_id": ..., "payment_method_id": ...} or None.
    Silently returns None if no Stripe method is configured.
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
    stripe_methods = [m for m in methods if m.get("type") == "stripe"]
    method = (
        next((m for m in stripe_methods if m["id"] == default_id), None)
        or (stripe_methods[0] if stripe_methods else None)
    )
    if not method:
        return None

    customer_id = method.get("customer_id")
    pm_id = method.get("payment_method_id")
    if not customer_id or not pm_id:
        return None

    return {"customer_id": customer_id, "payment_method_id": pm_id}


class ACPAdapter(MerchantAdapter):
    """Headless checkout via Stripe's Agentic Commerce Protocol (ACP).

    Requires a Stripe payment credential in ~/.shop/payment.yaml (type: stripe).
    Run `shop payment add` to set up a Stripe-backed payment method.

    Checkout flow:
      POST {acp_endpoint}/checkout — single-phase, synchronous
      Returns order_id + status on success.
    """

    def __init__(self, slug: str, config: dict) -> None:
        super().__init__(slug, config)
        self.acp_endpoint = config.get("acp_endpoint", "").rstrip("/")
        self.acp_key = config.get("acp_key", "")

    def _headers(self, idempotency_key: str) -> dict:
        h = {
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
            "Request-Id": str(uuid.uuid4()),
        }
        if self.acp_key:
            h["Authorization"] = f"Bearer {self.acp_key}"
        return h

    async def search(self, query: str, filters: SearchFilters) -> list[CommerceTXTProduct]:
        raise AdapterError(
            self.slug,
            "ACPAdapter handles checkout only. Use ShopifyCatalogAdapter or UCPAdapter for search.",
        )

    async def get_product(self, sku: str) -> CommerceTXTProduct:
        raise ProductNotFoundError(
            self.slug,
            "ACPAdapter does not expose product detail. Use ShopifyCatalogAdapter for search.",
        )

    async def get_capabilities(self) -> dict:
        return {
            "search": False,
            "order_create": True,
            "product_detail": False,
            "adapter": "acp",
            "acp_endpoint": self.acp_endpoint,
            "payment_handler": "stripe",
        }

    async def create_order(
        self,
        sku: str,
        quantity: int,
        mandate_id: str,
        idempotency_key: str,
        checkout_url: str | None = None,
    ) -> dict:
        """Place a headless order via the ACP checkout endpoint.

        Requires:
          - acp_endpoint configured in merchants.yaml
          - Stripe payment credential in ~/.shop/payment.yaml
        """
        if not self.acp_endpoint:
            raise AdapterError(self.slug, "No acp_endpoint configured")

        cred = _load_stripe_credential(_get_shop_dir())
        if not cred:
            raise AdapterError(
                self.slug,
                "No Stripe payment method configured. Run: shop payment add",
            )

        raw_sku = sku.removeprefix(f"{self.slug}:")

        payload = {
            "idempotency_key": idempotency_key,
            "items": [{"sku": raw_sku, "quantity": quantity}],
            "mandate_id": mandate_id,
            "payment": {
                "type": "stripe",
                "customer_id": cred["customer_id"],
                "payment_method_id": cred["payment_method_id"],
            },
        }

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                r = await client.post(
                    f"{self.acp_endpoint}/checkout",
                    json=payload,
                    headers=self._headers(idempotency_key),
                )
                if r.status_code == 409:
                    # Idempotent replay — return existing order
                    return r.json()
                if r.status_code in (501, 503):
                    raise CheckoutNotSupportedError(
                        self.slug, "Merchant ACP endpoint does not support checkout"
                    )
                if r.status_code == 402:
                    err = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                    raise AdapterError(
                        self.slug,
                        f"Payment declined: {err.get('message', r.text or 'no details')}",
                    )
                r.raise_for_status()
                result = r.json()
        except (AdapterError, CheckoutNotSupportedError):
            raise
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                raise AdapterError(
                    self.slug,
                    f"ACP auth error: HTTP {e.response.status_code} — check acp_key",
                )
            raise AdapterError(self.slug, f"ACP HTTP {e.response.status_code}")
        except httpx.TimeoutException:
            raise TimeoutError()
        except Exception as e:
            raise AdapterError(self.slug, str(e))

        order_id = result.get("order_id") or result.get("id")
        if not order_id:
            raise AdapterError(self.slug, f"No order_id in ACP checkout response: {result}")

        status = result.get("status", "confirmed")
        if status in ("rejected", "failed", "declined"):
            raise AdapterError(
                self.slug,
                f"ACP checkout rejected (status={status}): {result.get('message', '')}",
            )
        if status == "requires_action":
            raise CheckoutNotSupportedError(
                self.slug,
                f"ACP merchant requires human action: {result.get('action_url', 'no URL')}",
            )

        return {
            "order_id": order_id,
            "status": status,
            "total_cents": result.get("total_cents"),
            "currency": result.get("currency", "USD"),
            "confirmation_code": result.get("confirmation_code"),
            "adapter": "acp",
            "acp_endpoint": self.acp_endpoint,
        }
