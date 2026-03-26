"""ShopifyStorefrontAdapter — headless per-store Shopify checkout.

Uses the Shopify Storefront API (GraphQL) for fully headless purchasing:
  1. checkoutCreate  — creates a checkout session with line items
  2. Card vault      — vaults payment card at elb.deposit.shopifycs.com
  3. checkoutCompleteWithCreditCardV2 — submits payment and places order

Payment credentials are loaded from ~/.shop/payment.yaml (chmod 600).
Card details are vaulted per-checkout (single-use token); raw numbers
are never sent to Shopify directly.

Config keys (from merchants.yaml extra fields):
  store_domain            — e.g. my-store.myshopify.com
  storefront_access_token — public Storefront API access token
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import httpx
import yaml

from shop.adapters.base import AdapterError, CheckoutNotSupportedError, MerchantAdapter, ProductNotFoundError
from shop.models.commerce import CommerceTXTProduct, SearchFilters

_TIMEOUT = 10.0
_VAULT_URL = "https://elb.deposit.shopifycs.com/sessions"
_GQL_VERSION = "2024-01"


def _get_shop_dir() -> Path:
    return Path(os.environ["SHOP_HOME"]) if "SHOP_HOME" in os.environ else Path.home() / ".shop"


def _load_payment_config(shop_dir: Path) -> dict:
    """Load ~/.shop/payment.yaml. Raises AdapterError if missing or malformed."""
    payment_path = shop_dir / "payment.yaml"
    if not payment_path.exists():
        raise AdapterError(
            "payment",
            "No payment method configured. Run: shop payment add --help",
        )
    with payment_path.open() as f:
        data = yaml.safe_load(f) or {}

    methods = data.get("methods", [])
    if not methods:
        raise AdapterError("payment", "No payment methods in payment.yaml")

    default_id = data.get("default", methods[0]["id"])
    method = next((m for m in methods if m["id"] == default_id), methods[0])
    return method


def _extract_variant_id(checkout_url: str) -> str | None:
    """Extract Shopify variant GID from a checkout URL.

    Shopify cart URL format: https://store.myshopify.com/cart/VARIANT_ID:QTY
    """
    m = re.search(r"/cart/(\d+)(?::|$|\?)", checkout_url)
    if m:
        return f"gid://shopify/ProductVariant/{m.group(1)}"
    return None


async def _vault_card(card: dict, storefront_token: str) -> str:
    """Vault a credit card with Shopify's vaulting endpoint.

    Returns a single-use vault token (vaultId) for use in checkout.
    """
    payload = {
        "credit_card": {
            "number": str(card["number"]),
            "month": int(card["month"]),
            "year": int(card["year"]),
            "verification_value": str(card["cvv"]),
            "name": f"{card['first_name']} {card['last_name']}",
        }
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(
                _VAULT_URL,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Shopify-Storefront-Access-Token": storefront_token,
                },
            )
            r.raise_for_status()
            return r.json()["id"]
    except httpx.HTTPStatusError as e:
        raise AdapterError("shopify_vault", f"Card vaulting failed: HTTP {e.response.status_code}")
    except Exception as e:
        raise AdapterError("shopify_vault", f"Card vaulting error: {e}")


async def _gql(
    store_domain: str,
    storefront_token: str,
    query: str,
    variables: dict,
) -> dict:
    """Execute a Shopify Storefront GraphQL mutation."""
    url = f"https://{store_domain}/api/{_GQL_VERSION}/graphql.json"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(
                url,
                json={"query": query, "variables": variables},
                headers={
                    "Content-Type": "application/json",
                    "X-Shopify-Storefront-Access-Token": storefront_token,
                },
            )
            r.raise_for_status()
            return r.json()
    except httpx.HTTPStatusError as e:
        raise AdapterError(store_domain, f"Storefront API error: HTTP {e.response.status_code}")
    except httpx.TimeoutException:
        raise TimeoutError()
    except Exception as e:
        raise AdapterError(store_domain, str(e))


_CHECKOUT_CREATE = """
mutation checkoutCreate($input: CheckoutCreateInput!) {
  checkoutCreate(input: $input) {
    checkout {
      id
      webUrl
      totalPriceV2 { amount currencyCode }
    }
    checkoutUserErrors { code field message }
  }
}
"""

_CHECKOUT_COMPLETE = """
mutation checkoutCompleteWithCreditCardV2($checkoutId: ID!, $payment: CreditCardPaymentInputV2!) {
  checkoutCompleteWithCreditCardV2(checkoutId: $checkoutId, payment: $payment) {
    checkout {
      id
      completedAt
      order { id name }
    }
    payment {
      id
      ready
      errorMessage
    }
    checkoutUserErrors { code field message }
  }
}
"""


class ShopifyStorefrontAdapter(MerchantAdapter):
    """Headless per-store Shopify checkout via Storefront GraphQL API.

    Handles checkout only — search is handled by ShopifyCatalogAdapter.
    Payment credentials are read from ~/.shop/payment.yaml at order time.

    Config keys:
      store_domain            — my-store.myshopify.com
      storefront_access_token — public Storefront API token from store admin
    """

    def __init__(self, slug: str, config: dict) -> None:
        super().__init__(slug, config)
        self.store_domain = config.get("store_domain", "").strip()
        self.storefront_token = config.get("storefront_access_token", "")

    async def search(self, query: str, filters: SearchFilters) -> list[CommerceTXTProduct]:
        raise AdapterError(
            self.slug,
            "ShopifyStorefrontAdapter handles checkout only. Use ShopifyCatalogAdapter for search.",
        )

    async def get_product(self, sku: str) -> CommerceTXTProduct:
        raise ProductNotFoundError(
            self.slug,
            "ShopifyStorefrontAdapter does not expose product detail. Use ShopifyCatalogAdapter for search.",
        )

    async def get_capabilities(self) -> dict:
        return {
            "search": False,
            "order_create": True,
            "product_detail": False,
            "adapter": "shopify_storefront",
            "store_domain": self.store_domain,
        }

    async def create_order(
        self,
        sku: str,
        quantity: int,
        mandate_id: str,
        idempotency_key: str,
        checkout_url: str | None = None,
    ) -> dict:
        """Place a headless Shopify order.

        Requires checkout_url (from search results / cart) to identify the variant.
        Payment credentials are loaded from ~/.shop/payment.yaml.
        """
        if not self.store_domain or not self.storefront_token:
            raise AdapterError(self.slug, "store_domain and storefront_access_token required")

        # Resolve variant ID
        variant_id: str | None = None
        if checkout_url:
            variant_id = _extract_variant_id(checkout_url)
        if not variant_id:
            # Try to extract numeric ID from sku suffix if it looks like a variant ID
            sku_part = sku.split(":", 1)[-1] if ":" in sku else sku
            if sku_part.isdigit():
                variant_id = f"gid://shopify/ProductVariant/{sku_part}"
        if not variant_id:
            raise AdapterError(
                self.slug,
                "Cannot resolve Shopify variant ID — checkout_url required for Shopify orders.",
            )

        # Load payment config
        shop_dir = _get_shop_dir()
        card = _load_payment_config(shop_dir)
        billing = card.get("billing", {})
        shipping = card.get("shipping", billing)  # fall back to billing if no separate shipping

        def _addr(a: dict) -> dict:
            return {
                "firstName": card.get("first_name", ""),
                "lastName": card.get("last_name", ""),
                "address1": a.get("address1", ""),
                "city": a.get("city", ""),
                "province": a.get("province", ""),
                "country": a.get("country", "US"),
                "zip": a.get("zip", ""),
            }

        # Phase 1: create checkout
        checkout_input = {
            "lineItems": [{"variantId": variant_id, "quantity": quantity}],
            "email": card.get("email", "agent@shop-cli.dev"),
            "shippingAddress": _addr(shipping),
        }
        result = await _gql(
            self.store_domain, self.storefront_token, _CHECKOUT_CREATE,
            {"input": checkout_input},
        )
        errors = result.get("data", {}).get("checkoutCreate", {}).get("checkoutUserErrors", [])
        if errors:
            msgs = "; ".join(e["message"] for e in errors)
            raise AdapterError(self.slug, f"Checkout create failed: {msgs}")

        checkout = result["data"]["checkoutCreate"]["checkout"]
        checkout_id = checkout["id"]
        total_str = checkout.get("totalPriceV2", {}).get("amount", "0")
        try:
            total_amount = float(total_str)
        except (ValueError, TypeError):
            total_amount = 0.0

        # Phase 2: vault card
        vault_id = await _vault_card(card, self.storefront_token)

        # Phase 3: complete checkout
        payment_input = {
            "paymentAmountV2": {"amount": total_str, "currencyCode": "USD"},
            "idempotencyKey": idempotency_key,
            "billingAddress": _addr(billing),
            "vaultId": vault_id,
        }
        complete_result = await _gql(
            self.store_domain, self.storefront_token, _CHECKOUT_COMPLETE,
            {"checkoutId": checkout_id, "payment": payment_input},
        )
        complete_data = complete_result.get("data", {}).get("checkoutCompleteWithCreditCardV2", {})
        complete_errors = complete_data.get("checkoutUserErrors", [])
        if complete_errors:
            msgs = "; ".join(e["message"] for e in complete_errors)
            raise AdapterError(self.slug, f"Checkout complete failed: {msgs}")

        payment = complete_data.get("payment", {}) or {}
        if payment.get("errorMessage"):
            raise AdapterError(self.slug, f"Payment declined: {payment['errorMessage']}")

        completed_checkout = complete_data.get("checkout", {}) or {}
        order_info = completed_checkout.get("order") or {}
        shopify_order_id = order_info.get("id") or payment.get("id", "")
        shopify_order_name = order_info.get("name", "")

        return {
            "shopify_order_id": shopify_order_id,
            "shopify_order_name": shopify_order_name,
            "store_domain": self.store_domain,
            "checkout_id": checkout_id,
            "payment_id": payment.get("id"),
            "total_usd": total_amount,
            "completed_at": completed_checkout.get("completedAt"),
            "tracking": {
                "carrier": None,
                "tracking_number": None,
                "estimated_delivery": None,
            },
        }
