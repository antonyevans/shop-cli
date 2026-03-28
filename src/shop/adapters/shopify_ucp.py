"""ShopifyUCPAdapter — Shopify's agent checkout via UCP/MCP JSON-RPC protocol.

Shopify's checkout MCP server (`{shop}/api/ucp/mcp`) enables fully headless
agent purchasing without raw card details. Payment is via Shop Pay tokens.

Protocol:  JSON-RPC 2.0
Endpoint:  https://{store_domain}/api/ucp/mcp
Auth:      JWT from api.shopify.com/auth/access_token (same credentials
           as ShopifyCatalogAdapter — client_id + client_secret, 60-min TTL)
Payment:   Shop Pay token stored in ~/.shop/payment.yaml (type: shop_pay)

Checkout flow:
  1. create_checkout  — create session with line items + buyer email
  2. update_checkout  — add buyer address + Shop Pay payment instrument
  3. complete_checkout — submit with Shop Pay token credential
  4. Poll get_checkout if status is complete_in_progress

Status states: incomplete → ready_for_complete → complete_in_progress → completed
               requires_escalation → exit 5 (needs human)

Config keys (from merchants.yaml extra fields):
  store_domain   — e.g. my-store.myshopify.com
  client_id      — Shopify app client ID (same as catalog)
  client_secret  — Shopify app client secret (same as catalog)
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

_AUTH_URL = "https://api.shopify.com/auth/access_token"
_MCP_PATH = "/api/ucp/mcp"
_SHOP_PAY_HANDLER = "dev.shopify.shop_pay"
_TIMEOUT = 10.0
_POLL_INTERVAL = 2.0
_POLL_MAX_ATTEMPTS = 15


def _get_shop_dir() -> Path:
    return Path(os.environ["SHOP_HOME"]) if "SHOP_HOME" in os.environ else Path.home() / ".shop"


def _load_shop_pay_credential(shop_dir: Path) -> dict | None:
    """Load Shop Pay payment credential from payment.yaml, or None if unavailable."""
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
    shop_pay_methods = [m for m in methods if m.get("type") == "shop_pay"]
    method = (
        next((m for m in shop_pay_methods if m["id"] == default_id), None)
        or (shop_pay_methods[0] if shop_pay_methods else None)
    )
    if not method:
        return None

    token = method.get("shop_pay_token")
    if not token:
        return None

    return {
        "token": token,
        "email": method.get("email", ""),
        "billing_address": method.get("billing_address", {}),
    }


def _extract_variant_id(checkout_url: str) -> str | None:
    """Extract Shopify variant GID from a cart URL.

    Format: https://store.myshopify.com/cart/VARIANT_ID:QTY
    """
    import re
    m = re.search(r"/cart/(\d+)(?::|$|\?)", checkout_url)
    if m:
        return f"gid://shopify/ProductVariant/{m.group(1)}"
    return None


class ShopifyUCPAdapter(MerchantAdapter):
    """Headless Shopify checkout via Shopify's UCP/MCP JSON-RPC protocol.

    Uses Shop Pay tokens for payment — raw card details never required.
    Requires store_domain + client_id + client_secret in merchant config.
    Payment token: run `shop payment add-shop-pay` to store a Shop Pay token.
    """

    def __init__(self, slug: str, config: dict) -> None:
        super().__init__(slug, config)
        self.store_domain = config.get("store_domain", "").strip()
        self.client_id = config.get("client_id", "")
        self.client_secret = config.get("client_secret", "")
        self._jwt: str | None = None
        self._jwt_expires: float = 0.0

    async def _get_jwt(self) -> str:
        """Return a valid JWT, refreshing if expired (60-min TTL, refresh at 58 min)."""
        if self._jwt and time.monotonic() < self._jwt_expires:
            return self._jwt
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                r = await client.post(_AUTH_URL, json={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "client_credentials",
                })
                if r.status_code == 401:
                    raise AdapterError(self.slug, "Invalid Shopify credentials — check client_id/client_secret")
                r.raise_for_status()
                data = r.json()
        except AdapterError:
            raise
        except httpx.TimeoutException:
            raise TimeoutError()
        except Exception as e:
            raise AdapterError(self.slug, f"Shopify auth failed: {e}")

        self._jwt = data["access_token"]
        self._jwt_expires = time.monotonic() + 3480  # 58 min
        return self._jwt

    async def _rpc(self, method: str, params: dict) -> dict:
        """Execute a JSON-RPC 2.0 call against the Shopify UCP MCP server."""
        token = await self._get_jwt()
        url = f"https://{self.store_domain}{_MCP_PATH}"
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": params,
        }
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                r = await client.post(
                    url,
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {token}",
                    },
                )
                r.raise_for_status()
                result = r.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                raise AdapterError(self.slug, f"Shopify UCP auth error: HTTP {e.response.status_code} — check app scopes include checkout")
            raise AdapterError(self.slug, f"Shopify UCP HTTP {e.response.status_code}")
        except httpx.TimeoutException:
            raise TimeoutError()
        except Exception as e:
            raise AdapterError(self.slug, str(e))

        if "error" in result:
            err = result["error"]
            raise AdapterError(
                self.slug,
                f"UCP MCP error [{err.get('code', '?')}]: {err.get('message', err)}",
            )
        return result.get("result", {})

    async def search(self, query: str, filters: SearchFilters) -> list[CommerceTXTProduct]:
        raise AdapterError(
            self.slug,
            "ShopifyUCPAdapter handles checkout only. Use ShopifyCatalogAdapter for search.",
        )

    async def get_product(self, sku: str) -> CommerceTXTProduct:
        raise ProductNotFoundError(
            self.slug,
            "ShopifyUCPAdapter does not expose product detail. Use ShopifyCatalogAdapter for search.",
        )

    async def get_capabilities(self) -> dict:
        return {
            "search": False,
            "order_create": True,
            "product_detail": False,
            "adapter": "shopify_ucp",
            "store_domain": self.store_domain,
            "payment_handler": _SHOP_PAY_HANDLER,
        }

    async def create_order(
        self,
        sku: str,
        quantity: int,
        mandate_id: str,
        idempotency_key: str,
        checkout_url: str | None = None,
    ) -> dict:
        """Place a headless Shopify order via UCP/MCP JSON-RPC.

        Requires:
          - checkout_url (from search/cart) to identify the variant
          - Shop Pay token in ~/.shop/payment.yaml (type: shop_pay)
        """
        if not self.store_domain or not self.client_id or not self.client_secret:
            raise AdapterError(self.slug, "store_domain, client_id, and client_secret required")

        # Resolve variant ID
        variant_id: str | None = None
        if checkout_url:
            variant_id = _extract_variant_id(checkout_url)
        if not variant_id:
            sku_part = sku.split(":", 1)[-1] if ":" in sku else sku
            if sku_part.isdigit():
                variant_id = f"gid://shopify/ProductVariant/{sku_part}"
        if not variant_id:
            raise AdapterError(
                self.slug,
                "Cannot resolve Shopify variant ID — checkout_url required for Shopify UCP orders.",
            )

        # Load Shop Pay credentials
        shop_dir = _get_shop_dir()
        cred = _load_shop_pay_credential(shop_dir)
        if not cred:
            raise AdapterError(
                self.slug,
                "No Shop Pay token configured. Run: shop payment add-shop-pay --help",
            )

        shop_pay_token = cred["token"]
        buyer_email = cred.get("email") or "agent@shop-cli.dev"
        billing = cred.get("billing_address") or {}

        # Phase 1: create_checkout
        create_result = await self._rpc("create_checkout", {
            "checkout": {
                "currency": "USD",
                "line_items": [
                    {"quantity": quantity, "item": {"product_variant_id": variant_id}}
                ],
                "buyer": {"email": buyer_email},
            },
        })

        checkout_id = create_result.get("id")
        if not checkout_id:
            raise AdapterError(self.slug, f"No checkout ID in create_checkout response: {create_result}")

        status = create_result.get("status", "")
        if status == "requires_escalation":
            continue_url = create_result.get("continue_url", "")
            raise CheckoutNotSupportedError(
                self.slug,
                f"Shopify requires human intervention: {continue_url or 'no URL provided'}",
            )

        # Phase 2: update_checkout — add address + payment instrument
        # update_checkout requires the FULL checkout state (omitted fields are removed)
        existing_items = create_result.get("line_items", [
            {"quantity": quantity, "item": {"product_variant_id": variant_id}}
        ])
        update_idempotency = f"{idempotency_key}-update"

        update_result = await self._rpc("update_checkout", {
            "id": checkout_id,
            "checkout": {
                "currency": "USD",
                "line_items": existing_items,
                "buyer": {
                    "email": buyer_email,
                    **({"billing_address": billing} if billing else {}),
                },
                "payment": {
                    "handlers": [
                        {"handler_id": _SHOP_PAY_HANDLER, "type": "SHOP_PAY"}
                    ],
                    "instruments": [
                        {
                            "handler_id": _SHOP_PAY_HANDLER,
                            "type": "SHOP_PAY",
                            "credential": shop_pay_token,
                        }
                    ],
                },
            },
            "idempotency-key": update_idempotency,
        })

        status = update_result.get("status", "")
        if status == "requires_escalation":
            continue_url = update_result.get("continue_url", "")
            raise CheckoutNotSupportedError(
                self.slug,
                f"Shopify requires human intervention (payment step): {continue_url or 'no URL provided'}",
            )

        # Phase 3: complete_checkout
        complete_idempotency = f"{idempotency_key}-complete"

        complete_result = await self._rpc("complete_checkout", {
            "id": checkout_id,
            "payment": {
                "handler_id": _SHOP_PAY_HANDLER,
                "credential": shop_pay_token,
            },
            "idempotency-key": complete_idempotency,
        })

        # Poll if still in progress
        status = complete_result.get("status", "")
        attempts = 0
        while status == "complete_in_progress" and attempts < _POLL_MAX_ATTEMPTS:
            await _async_sleep(_POLL_INTERVAL)
            poll_result = await self._rpc("get_checkout", {"id": checkout_id})
            status = poll_result.get("status", "")
            complete_result = poll_result
            attempts += 1

        if status == "requires_escalation":
            continue_url = complete_result.get("continue_url", "")
            raise CheckoutNotSupportedError(
                self.slug,
                f"Shopify requires human intervention (complete step): {continue_url or 'no URL provided'}",
            )

        if status != "completed":
            messages = complete_result.get("messages", [])
            msg = "; ".join(m.get("message", str(m)) for m in messages) if messages else status
            raise AdapterError(self.slug, f"Checkout did not complete (status={status}): {msg}")

        order = complete_result.get("order") or {}
        return {
            "shopify_order_id": order.get("id") or complete_result.get("order_id", ""),
            "shopify_order_name": order.get("name") or order.get("order_number", ""),
            "store_domain": self.store_domain,
            "checkout_id": checkout_id,
            "status": "completed",
            "tracking": {
                "carrier": None,
                "tracking_number": None,
                "estimated_delivery": None,
            },
        }


async def _async_sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)
