"""Bolt integration tests.

Requires: SHOP_BOLT_API_KEY, SHOP_BOLT_MERCHANT_ID, SHOP_BOLT_TOKEN.

All tests run against Bolt sandbox. No real money moved.
"""

from __future__ import annotations

import os
import uuid

import pytest

from tests.integration._helpers import shop, skip_unless


pytestmark = [pytest.mark.integration, skip_unless(
    "SHOP_BOLT_API_KEY",
    "SHOP_BOLT_MERCHANT_ID",
    "SHOP_BOLT_TOKEN",
)]


class TestBolt:

    def test_sandbox_order_end_to_end(self, shop_home, mandate_id):
        """Single-phase Bolt checkout in sandbox."""
        shop(shop_home, "merchant", "add-bolt",
             "--name", "Bolt Sandbox",
             "--api-key", os.environ["SHOP_BOLT_API_KEY"],
             "--merchant-id", os.environ["SHOP_BOLT_MERCHANT_ID"],
             "--sandbox")

        shop(shop_home, "payment", "add-bolt",
             "--token", os.environ["SHOP_BOLT_TOKEN"],
             "--email", "test-buyer@example.com",
             "--label", "Bolt Sandbox")

        shop(shop_home, "cart", "add",
             "--sku", "bolt-sandbox:test-product",
             "--quantity", "1",
             "--price-usd", "9.99")

        result = shop(shop_home, "order", "create",
                      "--from-cart",
                      "--mandate-id", mandate_id,
                      "--idempotency-key", str(uuid.uuid4()),
                      "--yes")

        order = result["orders"][0]
        assert order["status"] == "completed"
        assert order["merchant"] == "bolt-sandbox"
        print(f"\nBolt order: {order}")
        print("Verify in Bolt Dashboard: https://merchant.bolt.com/")
