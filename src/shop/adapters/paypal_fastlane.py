"""PayPalFastlaneAdapter — checkout via PayPal Fastlane.

PayPal Fastlane is PayPal's headless checkout product for returning customers.
It stores payment + shipping details centrally; agents authenticate once and
use an opaque token to complete purchases at any Fastlane-enabled merchant.

Protocol:  REST/JSON (PayPal Orders API v2)
Auth:      OAuth 2.0 — merchant client_id + client_secret → access_token
Payment:   Fastlane payment token from ~/.shop/payment.yaml (type: paypal_fastlane)
Checkout:
  1. POST /v2/checkout/orders   — create order
  2. POST /v2/checkout/orders/{id}/capture — capture (charge) using Fastlane token

PayPal API base: https://api-m.paypal.com (live) / https://api-m.sandbox.paypal.com (sandbox)

Config keys (from merchants.yaml extra fields):
  paypal_client_id     — merchant's PayPal app client ID
  paypal_client_secret — merchant's PayPal app client secret
  paypal_sandbox       — "true" to use sandbox API (default: false)
  currency             — ISO 4217 currency code (default: USD)
"""

from __future__ import annotations

import os
import time
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

_LIVE_BASE = "https://api-m.paypal.com"
_SANDBOX_BASE = "https://api-m.sandbox.paypal.com"
_TIMEOUT = 10.0


def _get_shop_dir() -> Path:
    return Path(os.environ["SHOP_HOME"]) if "SHOP_HOME" in os.environ else Path.home() / ".shop"


def _load_fastlane_credential(shop_dir: Path) -> dict | None:
    """Load PayPal Fastlane token from payment.yaml.

    Returns {token, email, name} or None if no Fastlane method configured.
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
    fl_methods = [m for m in methods if m.get("type") == "paypal_fastlane"]
    method = (
        next((m for m in fl_methods if m["id"] == default_id), None)
        or (fl_methods[0] if fl_methods else None)
    )
    if not method:
        return None

    token = method.get("fastlane_token")
    if not token:
        return None

    return {
        "token": token,
        "email": method.get("email", ""),
        "name": method.get("name", ""),
        "billing_address": method.get("billing_address", {}),
    }


class PayPalFastlaneAdapter(MerchantAdapter):
    """Headless checkout via PayPal Fastlane.

    Uses the merchant's PayPal app credentials to create and capture orders.
    Payment is authorized via the buyer's stored Fastlane token — no raw
    card details are exposed to the agent.

    Requires `paypal_client_id` + `paypal_client_secret` in merchant config
    and a `paypal_fastlane` credential in ~/.shop/payment.yaml.
    Run `shop payment add-paypal-fastlane` to store a Fastlane token.
    """

    def __init__(self, slug: str, config: dict) -> None:
        super().__init__(slug, config)
        self.client_id = config.get("paypal_client_id", "")
        self.client_secret = config.get("paypal_client_secret", "")
        self.sandbox = str(config.get("paypal_sandbox", "")).lower() == "true"
        self.currency = config.get("currency", "USD").upper()
        self._base = _SANDBOX_BASE if self.sandbox else _LIVE_BASE
        self._access_token: str | None = None
        self._token_expires: float = 0.0

    async def _get_access_token(self) -> str:
        """Return a valid PayPal access token, refreshing if expired."""
        if self._access_token and time.monotonic() < self._token_expires:
            return self._access_token

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                r = await client.post(
                    f"{self._base}/v1/oauth2/token",
                    data={"grant_type": "client_credentials"},
                    auth=(self.client_id, self.client_secret),
                    headers={"Accept": "application/json"},
                )
                if r.status_code == 401:
                    raise AdapterError(
                        self.slug,
                        "Invalid PayPal client_id or client_secret",
                    )
                r.raise_for_status()
                data = r.json()
        except AdapterError:
            raise
        except httpx.TimeoutException:
            raise TimeoutError()
        except Exception as e:
            raise AdapterError(self.slug, f"PayPal auth failed: {e}")

        self._access_token = data["access_token"]
        expires_in = data.get("expires_in", 3600)
        self._token_expires = time.monotonic() + expires_in - 60  # refresh 1 min early
        return self._access_token

    def _auth_headers(self, token: str, idempotency_key: str) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "PayPal-Request-Id": idempotency_key,
            "Prefer": "return=representation",
        }

    async def _pp_post(self, token: str, path: str, body: dict, idempotency_key: str) -> dict:
        """POST to PayPal API with auth and idempotency headers."""
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                r = await client.post(
                    f"{self._base}{path}",
                    json=body,
                    headers=self._auth_headers(token, idempotency_key),
                )
                if r.status_code in (401, 403):
                    raise AdapterError(
                        self.slug,
                        f"PayPal auth error: HTTP {r.status_code} — check app credentials",
                    )
                if r.status_code == 422:
                    err = r.json()
                    detail = err.get("details", [{}])[0].get("description", r.text)
                    raise AdapterError(self.slug, f"PayPal rejected order: {detail}")
                r.raise_for_status()
                return r.json()
        except (AdapterError, CheckoutNotSupportedError):
            raise
        except httpx.TimeoutException:
            raise TimeoutError()
        except httpx.HTTPStatusError as e:
            raise AdapterError(self.slug, f"PayPal HTTP {e.response.status_code}")
        except Exception as e:
            raise AdapterError(self.slug, str(e))

    async def search(self, query: str, filters: SearchFilters) -> list[CommerceTXTProduct]:
        raise AdapterError(
            self.slug,
            "PayPalFastlaneAdapter handles checkout only. Use ShopifyCatalogAdapter for search.",
        )

    async def get_product(self, sku: str) -> CommerceTXTProduct:
        raise ProductNotFoundError(
            self.slug,
            "PayPalFastlaneAdapter does not expose product detail.",
        )

    async def get_capabilities(self) -> dict:
        return {
            "search": False,
            "order_create": True,
            "product_detail": False,
            "adapter": "paypal_fastlane",
            "sandbox": self.sandbox,
            "currency": self.currency,
            "payment_handler": "paypal_fastlane",
        }

    async def create_order(
        self,
        sku: str,
        quantity: int,
        mandate_id: str,
        idempotency_key: str,
        checkout_url: str | None = None,
    ) -> dict:
        """Place a headless order via PayPal Orders API v2 with Fastlane.

        Phase 1: POST /v2/checkout/orders — create CAPTURE-intent order
        Phase 2: POST /v2/checkout/orders/{id}/capture — charge Fastlane token
        """
        if not self.client_id or not self.client_secret:
            raise AdapterError(
                self.slug, "paypal_client_id and paypal_client_secret required in merchant config"
            )

        cred = _load_fastlane_credential(_get_shop_dir())
        if not cred:
            raise AdapterError(
                self.slug,
                "No PayPal Fastlane token configured. Run: shop payment add-paypal-fastlane",
            )

        token = await self._get_access_token()
        raw_sku = sku.removeprefix(f"{self.slug}:")

        # Phase 1: create order
        # Value is required — use a placeholder if no price known from sku.
        # In production, the calling layer should pass price via checkout_url metadata.
        order_body: dict = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "reference_id": raw_sku,
                    "custom_id": mandate_id,
                    "items": [
                        {
                            "name": raw_sku,
                            "quantity": str(quantity),
                            "unit_amount": {"currency_code": self.currency, "value": "0.00"},
                            "category": "PHYSICAL_GOODS",
                        }
                    ],
                    "amount": {
                        "currency_code": self.currency,
                        "value": "0.00",
                        "breakdown": {
                            "item_total": {"currency_code": self.currency, "value": "0.00"},
                        },
                    },
                }
            ],
            "payment_source": {
                "token": {
                    "id": cred["token"],
                    "type": "BILLING_AGREEMENT",
                }
            },
        }
        if cred.get("email"):
            order_body["payer"] = {"email_address": cred["email"]}

        create_idem = f"{idempotency_key}-create"
        order_result = await self._pp_post(token, "/v2/checkout/orders", order_body, create_idem)

        order_id = order_result.get("id")
        if not order_id:
            raise AdapterError(self.slug, f"No order ID in PayPal create response: {order_result}")

        order_status = order_result.get("status", "")
        if order_status in ("VOIDED", "PAYER_ACTION_REQUIRED"):
            if order_status == "PAYER_ACTION_REQUIRED":
                links = order_result.get("links", [])
                action_url = next((l["href"] for l in links if l.get("rel") == "payer-action"), "")
                raise CheckoutNotSupportedError(
                    self.slug,
                    f"PayPal requires buyer action: {action_url or 'check PayPal account'}",
                )
            raise AdapterError(self.slug, f"PayPal order voided (status={order_status})")

        # Phase 2: capture
        capture_idem = f"{idempotency_key}-capture"
        capture_result = await self._pp_post(
            token,
            f"/v2/checkout/orders/{order_id}/capture",
            {},
            capture_idem,
        )

        capture_status = capture_result.get("status", "")
        if capture_status not in ("COMPLETED", "APPROVED"):
            details = capture_result.get("purchase_units", [{}])[0]
            payments = details.get("payments", {})
            captures = payments.get("captures", [{}])
            cap_status = captures[0].get("status", capture_status) if captures else capture_status
            raise AdapterError(
                self.slug,
                f"PayPal capture did not complete (status={cap_status})",
            )

        purchase_units = capture_result.get("purchase_units", [{}])
        captures = purchase_units[0].get("payments", {}).get("captures", [{}])
        capture_id = captures[0].get("id", "") if captures else ""

        return {
            "paypal_order_id": order_id,
            "paypal_capture_id": capture_id,
            "status": "completed",
            "currency": self.currency,
            "adapter": "paypal_fastlane",
            "sandbox": self.sandbox,
        }
