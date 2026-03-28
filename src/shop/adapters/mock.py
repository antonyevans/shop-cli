"""MockAdapter — deterministic fixture-based adapter.

First-class v0 adapter (not dev-only). Enables offline development,
testing, and demo mode. Configure in merchants.yaml as adapter: mock.

Does NOT make any network calls.
"""

from __future__ import annotations

import importlib.resources
from datetime import datetime, timezone
from pathlib import Path

import yaml

from shop.adapters.base import MerchantAdapter, ProductNotFoundError
from shop.models.commerce import (
    CommerceTXTProduct,
    CommerceTXTReturns,
    CommerceTXTShipping,
    CommerceTXTTrust,
    SearchFilters,
)

_AVAILABILITY_MAP = {
    "InStock": "InStock",
    "OutOfStock": "OutOfStock",
    "PreOrder": "PreOrder",
    "Unknown": "Unknown",
}


def _load_fixture(fixture_path: str | None = None) -> list[dict]:
    """Load fixture catalog from YAML.

    Precedence:
    1. fixture_path from adapter config (absolute path)
    2. Bundled fixtures in src/shop/fixtures/mock_catalog.yaml
    """
    if fixture_path:
        path = Path(fixture_path).expanduser()
        with path.open() as f:
            data = yaml.safe_load(f)
        return data.get("products", [])

    # Fall back to bundled fixture
    pkg = importlib.resources.files("shop.fixtures")
    with (pkg / "mock_catalog.yaml").open() as f:
        data = yaml.safe_load(f)
    return data.get("products", [])


def _normalize(raw: dict, merchant_slug: str) -> CommerceTXTProduct:
    """Convert a fixture record to CommerceTXT format."""
    now = datetime.now(timezone.utc).isoformat()

    shipping_raw = raw.get("shipping") or {}
    returns_raw = raw.get("returns") or {}
    trust_raw = raw.get("trust") or {}
    history_raw = raw.get("price_history_30d")

    availability_raw = raw.get("availability", "Unknown")
    availability = _AVAILABILITY_MAP.get(availability_raw, "Unknown")

    return CommerceTXTProduct(
        sku=f"{merchant_slug}:{raw['sku']}",
        title=raw["title"],
        description=raw.get("description"),
        price=float(raw["price"]),
        price_history_30d=(
            {"min": float(history_raw["min"]), "max": float(history_raw["max"])}
            if history_raw
            else None
        ),
        availability=availability,
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
        cached_at=now,
        tax_excluded=True,
    )


class MockAdapter(MerchantAdapter):
    """Returns deterministic fixture data. No network calls."""

    def __init__(self, slug: str, config: dict) -> None:
        super().__init__(slug, config)
        fixture_path = config.get("fixture_file")
        raw_products = _load_fixture(fixture_path)
        self._products: list[CommerceTXTProduct] = [_normalize(p, slug) for p in raw_products]

    async def search(self, query: str, filters: SearchFilters) -> list[CommerceTXTProduct]:
        query_lower = query.lower()
        results = []
        for product in self._products:
            # Basic text match against title and description
            searchable = (product.title + " " + (product.description or "")).lower()
            if not any(term in searchable for term in query_lower.split()):
                continue

            # Apply filters
            if filters.max_price is not None and product.price > filters.max_price:
                continue
            if filters.min_rating is not None:
                rating = product.trust.seller_rating
                if rating is None or rating < filters.min_rating:
                    continue
            if filters.in_stock_only and product.availability != "InStock":
                continue

            results.append(product)

        return results

    async def get_product(self, sku: str) -> CommerceTXTProduct:
        for product in self._products:
            if product.sku == sku:
                return product
        raise ProductNotFoundError(f"SKU not found: {sku}")

    async def get_capabilities(self) -> dict:
        return {"search": True, "order_create": True}

    async def create_order(
        self,
        sku: str,
        quantity: int,
        mandate_id: str,
        idempotency_key: str,
        checkout_url: str | None = None,
    ) -> dict:
        import uuid

        product = await self.get_product(sku)
        return {
            "order_id": f"MOCK-{uuid.uuid4().hex[:8].upper()}",
            "status": "confirmed",
            "sku": sku,
            "quantity": quantity,
            "price_usd": product.price * quantity,
            "merchant": self.slug,
            "mandate_id": mandate_id,
            "idempotency_key": idempotency_key,
            "tracking": {"carrier": None, "tracking_number": None, "estimated_delivery": None},
        }
