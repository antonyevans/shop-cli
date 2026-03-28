"""ShopifyCatalogAdapter — global Shopify product search via Catalog REST API.

Covers ~1M+ Shopify merchants with a single credential set.
Auth: Dev Dashboard client_id + client_secret → 60-min JWT bearer token.

API reference: https://shopify.dev/docs/api/catalog-api/search
Token endpoint: https://api.shopify.com/auth/access_token
Search endpoint: https://discover.shopifyapps.com/global/v2/search
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime

import httpx

from shop.adapters.base import AdapterError, CheckoutNotSupportedError, MerchantAdapter, ProductNotFoundError
from shop.models.commerce import (
    CommerceTXTProduct,
    CommerceTXTReturns,
    CommerceTXTShipping,
    CommerceTXTTrust,
    SearchFilters,
)

_TOKEN_URL = "https://api.shopify.com/auth/access_token"
_SEARCH_URL = "https://discover.shopifyapps.com/global/v2/search"
_TIMEOUT = 5.0
_TOKEN_REFRESH_BUFFER = 60  # refresh this many seconds before expiry


class ShopifyCatalogAdapter(MerchantAdapter):
    """Global Shopify catalog search. One credential covers all eligible Shopify merchants.

    Config keys (from merchants.yaml extra fields):
      client_id     — Shopify Dev Dashboard app client ID
      client_secret — Shopify Dev Dashboard app client secret
      ships_to      — ISO 3166-1 alpha-2 country code (default: "US")
      limit         — max results per search call (1-10, default: 10)
    """

    def __init__(self, slug: str, config: dict) -> None:
        super().__init__(slug, config)
        self.client_id = config.get("client_id", "")
        self.client_secret = config.get("client_secret", "")
        self.ships_to = config.get("ships_to", "US")
        self.limit = min(int(config.get("limit", 10)), 10)
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    async def _get_token(self) -> str:
        """Return a valid bearer token, refreshing if within buffer window."""
        if self._token and time.time() < self._token_expires_at - _TOKEN_REFRESH_BUFFER:
            return self._token

        if not self.client_id or not self.client_secret:
            raise AdapterError(
                self.slug,
                "Shopify Catalog requires client_id and client_secret. "
                "Run: shop merchant connect shopify --client-id X --client-secret Y",
            )

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                r = await client.post(
                    _TOKEN_URL,
                    json={
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "grant_type": "client_credentials",
                    },
                )
                r.raise_for_status()
                data = r.json()
        except httpx.HTTPStatusError as e:
            raise AdapterError(self.slug, f"Shopify auth failed: HTTP {e.response.status_code}")
        except Exception as e:
            raise AdapterError(self.slug, f"Shopify auth error: {e}")

        self._token = data["access_token"]
        self._token_expires_at = time.time() + int(data.get("expires_in", 3600))
        return self._token

    async def search(self, query: str, filters: SearchFilters) -> list[CommerceTXTProduct]:
        token = await self._get_token()

        params: dict = {
            "query": query,
            "ships_to": self.ships_to,
            "available_for_sale": "1",
            "limit": str(self.limit),
        }
        if filters.max_price is not None:
            params["max_price"] = str(filters.max_price)
        if filters.min_rating is not None:
            params["min_rating"] = str(filters.min_rating)

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                r = await client.get(
                    _SEARCH_URL,
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
                r.raise_for_status()
                data = r.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                # Token may have been revoked — clear cache and signal error
                self._token = None
                raise AdapterError(self.slug, "Shopify auth token rejected; re-run shop merchant connect shopify")
            raise AdapterError(self.slug, f"Shopify Catalog search failed: HTTP {e.response.status_code}")
        except httpx.TimeoutException:
            raise TimeoutError()
        except Exception as e:
            raise AdapterError(self.slug, str(e))

        now = datetime.now(UTC).isoformat()
        # API returns a list directly (not wrapped in {"products": [...]})
        raw_list = data if isinstance(data, list) else data.get("products", [])
        products = []
        for raw in raw_list:
            try:
                normalized = self._normalize(raw, now)
                if normalized:
                    products.append(normalized)
            except Exception:
                continue

        return self._apply_filters(products, filters)

    async def get_product(self, sku: str) -> CommerceTXTProduct:
        raise ProductNotFoundError(
            self.slug,
            "Shopify Catalog API does not expose per-SKU product detail. "
            "Use search to discover products with their checkoutUrl.",
        )

    async def get_capabilities(self) -> dict:
        return {
            "search": True,
            "order_create": False,
            "product_detail": False,
            "adapter": "shopify_catalog",
        }

    async def create_order(
        self, sku: str, quantity: int, mandate_id: str, idempotency_key: str,
        checkout_url: str | None = None,
    ) -> dict:
        raise CheckoutNotSupportedError(
            self.slug,
            "ShopifyCatalogAdapter supports search only. "
            "Checkout flows through the merchant's native Shopify checkout (checkoutUrl in search results). "
            "Full Shopify checkout support is planned for v1.",
        )

    def _normalize(self, raw: dict, cached_at: str) -> CommerceTXTProduct | None:
        """Normalize a Shopify Catalog product record to CommerceTXT format."""
        # Shopify Catalog groups by UPID; each product has variants across merchants
        upid = raw.get("upid") or raw.get("id", "")
        title = raw.get("title", "")
        if not title:
            return None

        sku = f"{self.slug}:{upid}" if upid else None
        if not sku:
            return None

        # Pick the first available variant for pricing
        variants = raw.get("variants", [])
        if not variants:
            return None

        variant = variants[0]

        # Price: API returns {"amount": 1999, "currency": "USD"} in cents
        price_field = variant.get("price", {})
        if isinstance(price_field, dict):
            price = price_field.get("amount", 0) / 100.0
        else:
            try:
                price = float(price_field)
            except (ValueError, TypeError):
                price = 0.0

        available = variant.get("availableForSale", variant.get("available", True))
        availability = "InStock" if available else "OutOfStock"

        # Trust signals: rating is {"rating": 4.8, "count": 181} on variant or product
        rating_obj = variant.get("rating") or raw.get("rating") or {}
        if isinstance(rating_obj, dict):
            seller_rating = rating_obj.get("rating")
            review_count = rating_obj.get("count")
        else:
            seller_rating = rating_obj if rating_obj else None
            review_count = raw.get("reviews_count") or raw.get("review_count")

        # Vendor/merchant name from shop data on variant
        shop_info = variant.get("shop", {})
        vendor = shop_info.get("name", "") or raw.get("vendor", "")

        # Certifications from tags (may not be present in v2 API)
        tags = raw.get("tags", [])
        certs = [t for t in tags if t.startswith("cert:")] or None

        # Extract checkout URL and variant ID from variant data
        checkout_url = variant.get("checkoutUrl") or variant.get("checkout_url")
        variant_id: str | None = None
        if checkout_url:
            # Shopify cart URL format: https://store.myshopify.com/cart/VARIANT_ID:QTY
            m = re.search(r"/cart/(\d+)(?::|$)", checkout_url)
            if m:
                variant_id = f"gid://shopify/ProductVariant/{m.group(1)}"
        # Also try explicit variant ID fields (strip ?shop=xxx suffix if present)
        if not variant_id:
            raw_vid = variant.get("id") or variant.get("variantId")
            if raw_vid:
                vid_str = str(raw_vid).split("?")[0]  # strip ?shop=xxx
                if vid_str.startswith("gid://"):
                    variant_id = vid_str
                elif vid_str.isdigit():
                    variant_id = f"gid://shopify/ProductVariant/{vid_str}"

        return CommerceTXTProduct(
            sku=sku,
            title=title,
            description=raw.get("description") or raw.get("body_html"),
            price=price,
            price_history_30d=None,
            availability=availability,
            stock_count=None,
            shipping=CommerceTXTShipping(
                cost=None,
                window_days=None,
                carrier=None,
            ),
            returns=CommerceTXTReturns(
                window_days=None,
                restocking_fee=None,
                condition=None,
                refund_timeline_days=None,
            ),
            trust=CommerceTXTTrust(
                seller_rating=float(seller_rating) if seller_rating is not None else None,
                review_count=int(review_count) if review_count is not None else None,
                certifications=certs,
                authenticity=vendor or None,
            ),
            tax_excluded=True,
            cached_at=cached_at,
            checkout_url=checkout_url,
            variant_id=variant_id,
        )

    def _apply_filters(
        self, products: list[CommerceTXTProduct], filters: SearchFilters
    ) -> list[CommerceTXTProduct]:
        result = []
        for p in products:
            if filters.max_price is not None and p.price > filters.max_price:
                continue
            if filters.min_rating is not None:
                if p.trust.seller_rating is None or p.trust.seller_rating < filters.min_rating:
                    continue
            if filters.in_stock_only and p.availability != "InStock":
                continue
            result.append(p)
        return result
