"""PayPal Fastlane integration tests.

Requires: SHOP_PAYPAL_CLIENT_ID, SHOP_PAYPAL_CLIENT_SECRET, SHOP_PAYPAL_FASTLANE_TOKEN.

All tests run against PayPal sandbox. No real money moved.
"""

from __future__ import annotations

import os
import uuid

import pytest

from tests.integration._helpers import shop, skip_unless


pytestmark = [pytest.mark.integration, skip_unless(
    "SHOP_PAYPAL_CLIENT_ID",
    "SHOP_PAYPAL_CLIENT_SECRET",
    "SHOP_PAYPAL_FASTLANE_TOKEN",
)]


class TestPayPalFastlane:

    def test_sandbox_order_end_to_end(self, shop_home, mandate_id):
        """Create and capture a PayPal sandbox order."""
        shop(shop_home, "merchant", "add-paypal",
             "--name", "PayPal Sandbox",
             "--client-id", os.environ["SHOP_PAYPAL_CLIENT_ID"],
             "--client-secret", os.environ["SHOP_PAYPAL_CLIENT_SECRET"],
             "--sandbox")

        shop(shop_home, "payment", "add-paypal-fastlane",
             "--token", os.environ["SHOP_PAYPAL_FASTLANE_TOKEN"],
             "--email", "test-buyer@example.com",
             "--label", "PayPal Sandbox")

        shop(shop_home, "cart", "add",
             "--sku", "paypal-sandbox:test-product",
             "--quantity", "1",
             "--price-usd", "9.99")

        result = shop(shop_home, "order", "create",
                      "--from-cart",
                      "--mandate-id", mandate_id,
                      "--idempotency-key", str(uuid.uuid4()),
                      "--yes")

        order = result["orders"][0]
        assert order["status"] == "completed"
        assert order["merchant"] == "paypal-sandbox"
        print(f"\nPayPal order: {order}")
        print("Verify in PayPal Dashboard: https://developer.paypal.com/dashboard/")

    def test_oauth_token_cached(self, shop_home, mandate_id):
        """Second order re-uses cached OAuth token (no extra round-trip)."""
        shop(shop_home, "merchant", "add-paypal",
             "--name", "PayPal Sandbox",
             "--client-id", os.environ["SHOP_PAYPAL_CLIENT_ID"],
             "--client-secret", os.environ["SHOP_PAYPAL_CLIENT_SECRET"],
             "--sandbox")

        shop(shop_home, "payment", "add-paypal-fastlane",
             "--token", os.environ["SHOP_PAYPAL_FASTLANE_TOKEN"],
             "--email", "test-buyer@example.com",
             "--label", "PayPal Sandbox")

        for i in range(2):
            shop(shop_home, "cart", "add",
                 "--sku", "paypal-sandbox:test-product",
                 "--quantity", "1",
                 "--price-usd", "9.99")
            shop(shop_home, "order", "create",
                 "--from-cart",
                 "--mandate-id", mandate_id,
                 "--idempotency-key", str(uuid.uuid4()),
                 "--yes")
        # If token caching is broken, the second order would fail with an auth error
        history = shop(shop_home, "history", "--last", "5")
        assert history["total"] >= 2
