"""UCPAdapter — HTTP adapter for UCP-compatible merchants discovered via `shop merchant add`."""

from __future__ import annotations

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

_TIMEOUT = 3.0


class UCPAdapter(MerchantAdapter):
    """Adapter for UCP-compatible merchants.

    Expects the merchant endpoint to implement:
      GET  /capabilities           → {"search": bool, "order_create": bool}
      POST /search                 → {"products": [CommerceTXT ...]}
      GET  /products/{sku}         → CommerceTXT product JSON
      POST /orders                 → {"order_id": ..., "status": "confirmed", ...}
    """

    def __init__(self, slug: str, config: dict) -> None:
        super().__init__(slug, config)
        self.ucp_endpoint = config.get("ucp_endpoint", "").rstrip("/")

    async def search(self, query: str, filters: SearchFilters) -> list[CommerceTXTProduct]:
        if not self.ucp_endpoint:
            raise AdapterError(self.slug, "No ucp_endpoint configured")

        payload: dict = {"q": query}
        if filters.max_price is not None:
            payload["max_price"] = filters.max_price
        if filters.min_rating is not None:
            payload["min_rating"] = filters.min_rating
        if filters.in_stock_only:
            payload["in_stock_only"] = True

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                r = await client.post(f"{self.ucp_endpoint}/search", json=payload)
                r.raise_for_status()
                data = r.json()
        except httpx.TimeoutException:
            raise TimeoutError()
        except httpx.HTTPStatusError as e:
            raise AdapterError(self.slug, f"HTTP {e.response.status_code}")
        except Exception as e:
            raise AdapterError(self.slug, str(e))

        now = datetime.now(UTC).isoformat()
        products = []
        for raw in data.get("products", []):
            try:
                products.append(self._normalize(raw, now))
            except Exception:
                continue

        return self._apply_filters(products, filters)

    async def get_product(self, sku: str) -> CommerceTXTProduct:
        raw_sku = sku.removeprefix(f"{self.slug}:")
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                r = await client.get(f"{self.ucp_endpoint}/products/{raw_sku}")
                if r.status_code == 404:
                    raise ProductNotFoundError(sku)
                r.raise_for_status()
                data = r.json()
        except ProductNotFoundError:
            raise
        except httpx.TimeoutException:
            raise TimeoutError()
        except Exception as e:
            raise AdapterError(self.slug, str(e))

        return self._normalize(data, datetime.now(UTC).isoformat())

    async def get_capabilities(self) -> dict:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                r = await client.get(f"{self.ucp_endpoint}/capabilities")
                r.raise_for_status()
                return r.json()
        except Exception:
            return {}

    async def create_order(
        self, sku: str, quantity: int, mandate_id: str, idempotency_key: str
    ) -> dict:
        raw_sku = sku.removeprefix(f"{self.slug}:")
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                r = await client.post(
                    f"{self.ucp_endpoint}/orders",
                    json={
                        "sku": raw_sku,
                        "quantity": quantity,
                        "mandate_id": mandate_id,
                        "idempotency_key": idempotency_key,
                    },
                    headers={"Idempotency-Key": idempotency_key},
                )
                r.raise_for_status()
                return r.json()
        except httpx.TimeoutException:
            raise TimeoutError()
        except Exception as e:
            raise AdapterError(self.slug, str(e))

    def _normalize(self, raw: dict, cached_at: str) -> CommerceTXTProduct:
        sku = raw.get("sku", "")
        if not sku.startswith(f"{self.slug}:"):
            sku = f"{self.slug}:{sku}"

        shipping_raw = raw.get("shipping") or {}
        returns_raw = raw.get("returns") or {}
        trust_raw = raw.get("trust") or {}

        avail = raw.get("availability", "InStock")
        if avail not in ("InStock", "OutOfStock", "PreOrder"):
            avail = "InStock" if avail.lower() in ("in_stock", "in-stock", "available") else "OutOfStock"

        return CommerceTXTProduct(
            sku=sku,
            title=raw.get("title", ""),
            description=raw.get("description"),
            price=float(raw.get("price", 0)),
            price_history_30d=raw.get("price_history_30d"),
            availability=avail,
            stock_count=raw.get("stock_count"),
            shipping=CommerceTXTShipping(
                cost=shipping_raw.get("cost"),
                window_days=shipping_raw.get("window_days"),
                carrier=shipping_raw.get("carrier"),
            ),
            returns=CommerceTXTReturns(
                window_days=returns_raw.get("window_days"),
                restocking_fee=returns_raw.get("restocking_fee"),
                condition=returns_raw.get("condition"),
                refund_timeline_days=returns_raw.get("refund_timeline_days"),
            ),
            trust=CommerceTXTTrust(
                seller_rating=trust_raw.get("seller_rating"),
                review_count=trust_raw.get("review_count"),
                certifications=trust_raw.get("certifications"),
                authenticity=trust_raw.get("authenticity"),
            ),
            tax_excluded=raw.get("tax_excluded", True),
            cached_at=raw.get("cached_at", cached_at),
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
