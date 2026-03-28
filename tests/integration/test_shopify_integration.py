"""Shopify integration tests.

Tier 0: Catalog search — requires SHOP_SHOPIFY_CLIENT_ID + SHOP_SHOPIFY_CLIENT_SECRET.
Tier 3: UCP checkout — requires above + SHOP_SHOPIFY_SHOP_PAY_TOKEN.

Catalog search is "always on" once Shopify credentials are set — no purchases made.
"""

from __future__ import annotations

import os

import pytest

from tests.integration._helpers import shop, skip_unless


pytestmark = pytest.mark.integration

_HAS_CATALOG_CREDS = bool(
    os.environ.get("SHOP_SHOPIFY_CLIENT_ID")
    and os.environ.get("SHOP_SHOPIFY_CLIENT_SECRET")
)


@skip_unless("SHOP_SHOPIFY_CLIENT_ID", "SHOP_SHOPIFY_CLIENT_SECRET")
class TestShopifyCatalogSearch:
    """Shopify Catalog search — read-only, no purchases. Safe to run on any schedule."""

    def test_search_returns_results(self, shop_home):
        """Connect catalog and search for a common product."""
        shop(shop_home, "merchant", "connect-shopify",
             "--client-id", os.environ["SHOP_SHOPIFY_CLIENT_ID"],
             "--client-secret", os.environ["SHOP_SHOPIFY_CLIENT_SECRET"])

        # Exit 5 is acceptable: results returned but below confidence threshold
        # because catalog API doesn't include return policy data
        result = shop(shop_home, "search", "products", "coffee filters",
                      "--max-price", "50",
                      expect_exit=5)

        # Results should exist even if confidence is low
        assert "results" in result
        assert len(result["results"]) > 0

    def test_search_result_fields(self, shop_home):
        """Search results have required CommerceTXT fields."""
        shop(shop_home, "merchant", "connect-shopify",
             "--client-id", os.environ["SHOP_SHOPIFY_CLIENT_ID"],
             "--client-secret", os.environ["SHOP_SHOPIFY_CLIENT_SECRET"])

        result = shop(shop_home, "search", "products", "coffee",
                      expect_exit=5)

        if result.get("results"):
            first = result["results"][0]
            assert "sku" in first
            assert "title" in first
            assert "price" in first
            assert "availability" in first
            assert "confidence" in first

    def test_search_max_price_filter(self, shop_home):
        """max_price filter removes expensive results."""
        shop(shop_home, "merchant", "connect-shopify",
             "--client-id", os.environ["SHOP_SHOPIFY_CLIENT_ID"],
             "--client-secret", os.environ["SHOP_SHOPIFY_CLIENT_SECRET"])

        result = shop(shop_home, "search", "products", "coffee",
                      "--max-price", "10",
                      expect_exit=5)

        for item in result.get("results", []):
            assert item["price"] <= 10.0, f"Price {item['price']} exceeds max_price=10"


@skip_unless("SHOP_SHOPIFY_CLIENT_ID", "SHOP_SHOPIFY_CLIENT_SECRET", "SHOP_SHOPIFY_SHOP_PAY_TOKEN")
class TestShopifyUCPCheckout:
    """Shopify UCP / Shop Pay checkout — requires store password disabled and checkout scope."""

    def test_ucp_checkout_end_to_end(self, shop_home, mandate_id):
        """Search for a product, add to cart, place order via Shop Pay."""
        shop(shop_home, "merchant", "connect-shopify",
             "--client-id", os.environ["SHOP_SHOPIFY_CLIENT_ID"],
             "--client-secret", os.environ["SHOP_SHOPIFY_CLIENT_SECRET"])

        shop(shop_home, "merchant", "add-shopify-checkout",
             "--store-domain", "shop-cli-test.myshopify.com")

        shop(shop_home, "payment", "add-shop-pay",
             "--token", os.environ["SHOP_SHOPIFY_SHOP_PAY_TOKEN"],
             "--email", "test@example.com",
             "--first-name", "Test",
             "--last-name", "Buyer",
             "--address1", "123 Test St",
             "--city", "Ottawa",
             "--province", "ON",
             "--zip", "K1A0A1",
             "--country", "CA",
             "--label", "Shop Pay Test")

        # Search for a product to get a real SKU + checkout URL
        search = shop(shop_home, "search", "products", "test product",
                      "--max-price", "50",
                      expect_exit=5)

        assert search.get("results"), "No search results — is store password still enabled?"
        product = search["results"][0]

        shop(shop_home, "cart", "add",
             "--sku", product["sku"],
             "--checkout-url", product.get("checkout_url", ""),
             "--quantity", "1")

        import uuid
        result = shop(shop_home, "order", "create",
                      "--from-cart",
                      "--mandate-id", mandate_id,
                      "--idempotency-key", str(uuid.uuid4()),
                      "--yes")

        assert result["orders"][0]["status"] in ("completed", "confirmed")
        print(f"\nShopify UCP order: {result}")
        print("Verify in Shopify Admin: https://shop-cli-test.myshopify.com/admin/orders")
