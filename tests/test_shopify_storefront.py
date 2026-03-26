"""Tests for ShopifyStorefrontAdapter — headless Shopify checkout."""

from __future__ import annotations

import pytest
import respx
import httpx

from shop.adapters.shopify_storefront import (
    ShopifyStorefrontAdapter,
    _extract_variant_id,
    _VAULT_URL,
)
from shop.adapters.base import AdapterError, CheckoutNotSupportedError, ProductNotFoundError
from shop.models.commerce import SearchFilters


_STORE = "my-store.myshopify.com"
_TOKEN = "test_storefront_token"
_GQL_URL = f"https://{_STORE}/api/2024-01/graphql.json"

_CARD = {
    "id": "card_abc",
    "label": "Test Visa",
    "type": "credit_card",
    "number": "4242424242424242",
    "first_name": "Test",
    "last_name": "User",
    "month": 12,
    "year": 2026,
    "cvv": "123",
    "email": "agent@shop-cli.dev",
    "billing": {"address1": "1 Main St", "city": "NYC", "province": "NY", "country": "US", "zip": "10001"},
}

_CHECKOUT_URL = f"https://{_STORE}/cart/1234567890:1"


def _make_adapter() -> ShopifyStorefrontAdapter:
    return ShopifyStorefrontAdapter(
        slug="my-store-myshopify-com",
        config={"store_domain": _STORE, "storefront_access_token": _TOKEN},
    )


def _gql_checkout_create_ok() -> dict:
    return {
        "data": {
            "checkoutCreate": {
                "checkout": {
                    "id": "gid://shopify/Checkout/abc123",
                    "webUrl": f"https://{_STORE}/checkouts/abc123",
                    "totalPriceV2": {"amount": "12.99", "currencyCode": "USD"},
                },
                "checkoutUserErrors": [],
            }
        }
    }


def _gql_checkout_complete_ok() -> dict:
    return {
        "data": {
            "checkoutCompleteWithCreditCardV2": {
                "checkout": {
                    "id": "gid://shopify/Checkout/abc123",
                    "completedAt": "2026-03-25T12:00:00Z",
                    "order": {"id": "gid://shopify/Order/999", "name": "#1001"},
                },
                "payment": {"id": "gid://shopify/Payment/555", "ready": True, "errorMessage": None},
                "checkoutUserErrors": [],
            }
        }
    }


class TestExtractVariantId:
    def test_cart_url_format(self):
        url = f"https://{_STORE}/cart/1234567890:1"
        assert _extract_variant_id(url) == "gid://shopify/ProductVariant/1234567890"

    def test_cart_url_with_query(self):
        url = f"https://{_STORE}/cart/9876543210:2?channel=buy_button"
        assert _extract_variant_id(url) == "gid://shopify/ProductVariant/9876543210"

    def test_non_cart_url_returns_none(self):
        assert _extract_variant_id("https://store.myshopify.com/products/coffee") is None


class TestUnsupportedOperations:
    @pytest.mark.asyncio
    async def test_search_raises(self):
        adapter = _make_adapter()
        with pytest.raises(AdapterError):
            await adapter.search("coffee", SearchFilters())

    @pytest.mark.asyncio
    async def test_get_product_raises(self):
        adapter = _make_adapter()
        with pytest.raises(ProductNotFoundError):
            await adapter.get_product("shopify:123")

    @pytest.mark.asyncio
    async def test_get_capabilities_returns_order_create_true(self):
        adapter = _make_adapter()
        caps = await adapter.get_capabilities()
        assert caps["order_create"] is True
        assert caps["search"] is False


class TestCreateOrder:
    @pytest.mark.asyncio
    async def test_successful_order(self, tmp_path):
        adapter = _make_adapter()

        # Write payment config
        import yaml
        payment_path = tmp_path / "payment.yaml"
        payment_path.write_text(yaml.dump({"default": "card_abc", "methods": [_CARD]}))
        payment_path.chmod(0o600)

        import os
        os.environ["SHOP_HOME"] = str(tmp_path)

        try:
            with respx.mock:
                respx.post(_VAULT_URL).mock(return_value=httpx.Response(200, json={"id": "vault_tok_123"}))
                respx.post(_GQL_URL).mock(
                    side_effect=[
                        httpx.Response(200, json=_gql_checkout_create_ok()),
                        httpx.Response(200, json=_gql_checkout_complete_ok()),
                    ]
                )

                result = await adapter.create_order(
                    sku="shopify:1234567890",
                    quantity=1,
                    mandate_id="m-123",
                    idempotency_key="idem-key",
                    checkout_url=_CHECKOUT_URL,
                )
        finally:
            os.environ.pop("SHOP_HOME", None)

        assert result["shopify_order_name"] == "#1001"
        assert result["total_usd"] == 12.99
        assert result["completed_at"] == "2026-03-25T12:00:00Z"

    @pytest.mark.asyncio
    async def test_missing_checkout_url_raises(self, tmp_path):
        adapter = _make_adapter()

        import yaml, os
        payment_path = tmp_path / "payment.yaml"
        payment_path.write_text(yaml.dump({"default": "card_abc", "methods": [_CARD]}))
        os.environ["SHOP_HOME"] = str(tmp_path)

        try:
            with pytest.raises(AdapterError, match="variant ID"):
                await adapter.create_order("shopify:abc", 1, "m-123", "idem", checkout_url=None)
        finally:
            os.environ.pop("SHOP_HOME", None)

    @pytest.mark.asyncio
    async def test_missing_payment_config_raises(self, tmp_path):
        adapter = _make_adapter()

        import os
        os.environ["SHOP_HOME"] = str(tmp_path)
        try:
            with pytest.raises(AdapterError, match="payment"):
                await adapter.create_order("shopify:123", 1, "m-123", "idem", checkout_url=_CHECKOUT_URL)
        finally:
            os.environ.pop("SHOP_HOME", None)

    @pytest.mark.asyncio
    async def test_checkout_user_errors_raises(self, tmp_path):
        adapter = _make_adapter()

        import yaml, os
        payment_path = tmp_path / "payment.yaml"
        payment_path.write_text(yaml.dump({"default": "card_abc", "methods": [_CARD]}))
        os.environ["SHOP_HOME"] = str(tmp_path)

        error_response = {
            "data": {
                "checkoutCreate": {
                    "checkout": None,
                    "checkoutUserErrors": [{"code": "INVALID", "field": "variantId", "message": "Variant not found"}],
                }
            }
        }

        try:
            with respx.mock:
                respx.post(_GQL_URL).mock(return_value=httpx.Response(200, json=error_response))
                with pytest.raises(AdapterError, match="Variant not found"):
                    await adapter.create_order("shopify:1234567890", 1, "m-123", "idem", checkout_url=_CHECKOUT_URL)
        finally:
            os.environ.pop("SHOP_HOME", None)

    @pytest.mark.asyncio
    async def test_payment_declined_raises(self, tmp_path):
        adapter = _make_adapter()

        import yaml, os
        payment_path = tmp_path / "payment.yaml"
        payment_path.write_text(yaml.dump({"default": "card_abc", "methods": [_CARD]}))
        os.environ["SHOP_HOME"] = str(tmp_path)

        declined_response = {
            "data": {
                "checkoutCompleteWithCreditCardV2": {
                    "checkout": {"id": "gid://shopify/Checkout/abc", "completedAt": None, "order": None},
                    "payment": {"id": "pay_1", "ready": False, "errorMessage": "Card declined"},
                    "checkoutUserErrors": [],
                }
            }
        }

        try:
            with respx.mock:
                respx.post(_VAULT_URL).mock(return_value=httpx.Response(200, json={"id": "vault_tok"}))
                respx.post(_GQL_URL).mock(
                    side_effect=[
                        httpx.Response(200, json=_gql_checkout_create_ok()),
                        httpx.Response(200, json=declined_response),
                    ]
                )
                with pytest.raises(AdapterError, match="declined"):
                    await adapter.create_order("shopify:1234567890", 1, "m-123", "idem", checkout_url=_CHECKOUT_URL)
        finally:
            os.environ.pop("SHOP_HOME", None)
