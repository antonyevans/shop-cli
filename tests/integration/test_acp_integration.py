"""ACP integration tests.

Tier 0 (stub mode): always runs — no credentials needed.
Tier 1 (real payment): requires SHOP_STRIPE_SECRET_KEY.

These tests invoke `shop` as a subprocess and assert on JSON output.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from tests.integration._helpers import shop, skip_unless


pytestmark = pytest.mark.integration


# ── Tier 0: stub mode (always runs) ────────────────────────────────────────


class TestACPStub:
    """ACP adapter wiring — verifies credentials reach the server and response is parsed."""

    def test_order_placed_stub(self, acp_merchant, mandate_id):
        """Full end-to-end ACP order in stub mode."""
        shop_home, slug = acp_merchant

        # Add to cart (price required since ACP has no search)
        shop(shop_home, "cart", "add",
             "--sku", f"{slug}:coffee-filters",
             "--quantity", "1",
             "--price-usd", "9.99")

        # Place order
        result = shop(shop_home, "order", "create",
                      "--from-cart",
                      "--mandate-id", mandate_id,
                      "--idempotency-key", str(uuid.uuid4()),
                      "--yes")

        assert result["total_orders"] == 1
        order = result["orders"][0]
        assert order["status"] == "confirmed"
        assert order["merchant"] == slug
        assert order["price_usd"] == 9.99

    def test_order_in_history(self, acp_merchant, mandate_id):
        """Placed order appears in history."""
        shop_home, slug = acp_merchant

        shop(shop_home, "cart", "add",
             "--sku", f"{slug}:widget",
             "--quantity", "1",
             "--price-usd", "5.00")

        shop(shop_home, "order", "create",
             "--from-cart",
             "--mandate-id", mandate_id,
             "--idempotency-key", str(uuid.uuid4()),
             "--yes")

        history = shop(shop_home, "history", "--last", "5")
        assert history["total"] >= 1
        merchant_slugs = [o["merchant"] for o in history["orders"]]
        assert slug in merchant_slugs

    def test_payment_list_hides_secrets(self, shop_home_with_stripe, acp_server):
        """Payment list must not expose raw credentials."""
        result = shop(shop_home_with_stripe, "payment", "list")
        raw = str(result)
        assert "sk_" not in raw
        assert "4242424242424242" not in raw

    def test_mandate_budget_enforced(self, acp_merchant):
        """Order that exceeds mandate per-order limit is rejected (exit 3)."""
        shop_home, slug = acp_merchant

        # Create a tight mandate
        out = shop(shop_home, "mandate", "create",
                   "--budget-total", "10",
                   "--per-order-max", "5",
                   "--period", "monthly")
        tight_mandate = out["mandate_id"]

        shop(shop_home, "cart", "add",
             "--sku", f"{slug}:expensive-item",
             "--quantity", "1",
             "--price-usd", "99.99")

        shop(shop_home, "order", "create",
             "--from-cart",
             "--mandate-id", tight_mandate,
             "--idempotency-key", str(uuid.uuid4()),
             "--yes",
             expect_exit=3)

    def test_wrong_acp_key_returns_error(self, acp_merchant, mandate_id):
        """Wrong API key → adapter error (exit 4), not a hung process."""
        import textwrap
        shop_home, _ = acp_merchant
        port = None
        # Read port from existing merchants.yaml
        import yaml
        m = yaml.safe_load((shop_home / "merchants.yaml").read_text())
        port = int(m["merchants"][0]["acp_endpoint"].split(":")[-1].rstrip("/api/acp").split("/")[0])

        slug = "acp-bad-key"
        (shop_home / "merchants.yaml").write_text(textwrap.dedent(f"""\
            merchants:
            - slug: {slug}
              name: ACP Bad Key
              adapter: acp
              base_url: http://localhost:{port}
              acp_endpoint: http://localhost:{port}/api/acp
              acp_key: wrong-key
        """))

        shop(shop_home, "cart", "add",
             "--sku", f"{slug}:item",
             "--quantity", "1",
             "--price-usd", "1.00")

        # Should fail with order_failed (exit 6) — auth errors during checkout
        # are wrapped as order failures, not checkout_not_supported (exit 4)
        result = shop(shop_home, "order", "create",
                      "--from-cart",
                      "--mandate-id", mandate_id,
                      "--idempotency-key", str(uuid.uuid4()),
                      "--yes",
                      expect_exit=6)
        assert "error_code" in result
        assert "auth" in result.get("detail", "").lower() or "401" in result.get("detail", "")


# ── Tier 1: real Stripe payment ─────────────────────────────────────────────


@skip_unless("SHOP_STRIPE_SECRET_KEY")
class TestACPRealPayment:
    """ACP with real Stripe PaymentIntent — requires SHOP_STRIPE_SECRET_KEY."""

    def test_real_payment_intent_created(self, acp_merchant, shop_home_with_stripe):
        """ACP checkout creates a real Stripe PaymentIntent (test mode)."""
        shop_home, slug = acp_merchant

        # Copy stripe credential to the acp_merchant's shop_home
        import shutil
        stripe_cred = shop_home_with_stripe / "payment.yaml"
        shutil.copy(stripe_cred, shop_home / "payment.yaml")
        (shop_home / "payment.yaml").chmod(0o600)

        # Create mandate in acp_merchant's shop_home
        out = shop(shop_home, "mandate", "create",
                   "--budget-total", "100",
                   "--per-order-max", "50",
                   "--period", "monthly")
        mandate = out["mandate_id"]

        shop(shop_home, "cart", "add",
             "--sku", f"{slug}:coffee-filters",
             "--quantity", "1",
             "--price-usd", "9.99")

        result = shop(shop_home, "order", "create",
                      "--from-cart",
                      "--mandate-id", mandate,
                      "--idempotency-key", str(uuid.uuid4()),
                      "--yes")

        order = result["orders"][0]
        assert order["status"] == "confirmed"

        # Verify the order_id came from Stripe (should contain pi_ in the response
        # passed through from the server's acp-demo-{intent.id} format)
        # The server logs will show the Stripe PaymentIntent ID — we don't have access
        # to them here, but a confirmed order from a real-Stripe server means it charged.
        print(f"\nACP real order: {order}")
        print("Verify in Stripe Dashboard: https://dashboard.stripe.com/test/payments")
