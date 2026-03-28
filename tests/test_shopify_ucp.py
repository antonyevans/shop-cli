"""Tests for ShopifyUCPAdapter — Shopify's agent checkout via UCP/MCP JSON-RPC."""

from __future__ import annotations

import json
import os
import pytest
import respx
import httpx
import yaml

from shop.adapters.shopify_ucp import (
    ShopifyUCPAdapter,
    _extract_variant_id,
    _load_shop_pay_credential,
    _AUTH_URL,
    _MCP_PATH,
)
from shop.adapters.base import AdapterError, CheckoutNotSupportedError, ProductNotFoundError
from shop.models.commerce import SearchFilters

_STORE = "shop-cli-test.myshopify.com"
_MCP_URL = f"https://{_STORE}{_MCP_PATH}"
_CLIENT_ID = "test_client_id"
_CLIENT_SECRET = "test_client_secret"
_JWT = "eyJhbGciOiJFUzI1NiJ9.test.token"

_SHOP_PAY_TOKEN = "spay_tok_test_abc123"
_CHECKOUT_ID = "gid://shopify/Checkout/abc123"
_VARIANT_ID = "gid://shopify/ProductVariant/62667067457907"

_JWT_RESPONSE = {"access_token": _JWT, "token_type": "Bearer"}

_CREATE_RESPONSE = {
    "id": _CHECKOUT_ID,
    "status": "incomplete",
    "line_items": [{"quantity": 1, "item": {"product_variant_id": _VARIANT_ID}}],
    "messages": [],
}

_UPDATE_RESPONSE = {
    "id": _CHECKOUT_ID,
    "status": "ready_for_complete",
    "line_items": [{"quantity": 1, "item": {"product_variant_id": _VARIANT_ID}}],
    "messages": [],
}

_COMPLETE_RESPONSE = {
    "id": _CHECKOUT_ID,
    "status": "completed",
    "order": {"id": "gid://shopify/Order/999", "name": "#1001"},
    "messages": [],
}


def _make_adapter() -> ShopifyUCPAdapter:
    return ShopifyUCPAdapter(
        slug="shop-cli-test-myshopify-com",
        config={
            "store_domain": _STORE,
            "client_id": _CLIENT_ID,
            "client_secret": _CLIENT_SECRET,
        },
    )


def _rpc_response(result: dict, rpc_id: str = "1") -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _rpc_error(code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": "1", "error": {"code": code, "message": message}}


def _write_shop_pay(tmp_path, token=_SHOP_PAY_TOKEN, email="agent@shop-cli.dev"):
    data = {
        "default": "shoppay_test01",
        "methods": [{
            "id": "shoppay_test01",
            "label": "Shop Pay",
            "type": "shop_pay",
            "email": email,
            "shop_pay_token": token,
            "billing_address": {
                "first_name": "Test",
                "last_name": "Agent",
                "street_address": "1 Test St",
                "address_locality": "Boston",
                "address_region": "MA",
                "postal_code": "02101",
                "address_country": "US",
            },
        }],
        "pending": [],
    }
    p = tmp_path / "payment.yaml"
    p.write_text(yaml.dump(data))
    p.chmod(0o600)


class TestExtractVariantId:
    def test_cart_url(self):
        url = f"https://{_STORE}/cart/62667067457907:1"
        assert _extract_variant_id(url) == _VARIANT_ID

    def test_cart_url_with_query(self):
        url = f"https://{_STORE}/cart/62667067457907:2?ref=buy"
        assert _extract_variant_id(url) == _VARIANT_ID

    def test_non_cart_url_returns_none(self):
        assert _extract_variant_id(f"https://{_STORE}/products/snowboard") is None


class TestLoadShopPayCredential:
    def test_returns_token_for_shop_pay_method(self, tmp_path):
        _write_shop_pay(tmp_path)
        cred = _load_shop_pay_credential(tmp_path)
        assert cred["token"] == _SHOP_PAY_TOKEN
        assert cred["email"] == "agent@shop-cli.dev"

    def test_returns_none_if_no_file(self, tmp_path):
        assert _load_shop_pay_credential(tmp_path) is None

    def test_returns_none_for_stripe_method(self, tmp_path):
        data = {
            "default": "pm_abc",
            "methods": [{"id": "pm_abc", "type": "stripe", "payment_method_id": "pm_abc"}],
        }
        (tmp_path / "payment.yaml").write_text(yaml.dump(data))
        assert _load_shop_pay_credential(tmp_path) is None

    def test_returns_none_if_token_missing(self, tmp_path):
        data = {
            "default": "sp_1",
            "methods": [{"id": "sp_1", "type": "shop_pay", "email": "x@x.com"}],  # no token
        }
        (tmp_path / "payment.yaml").write_text(yaml.dump(data))
        assert _load_shop_pay_credential(tmp_path) is None


class TestUnsupportedOperations:
    @pytest.mark.asyncio
    async def test_search_raises(self):
        with pytest.raises(AdapterError):
            await _make_adapter().search("coffee", SearchFilters())

    @pytest.mark.asyncio
    async def test_get_product_raises(self):
        with pytest.raises(ProductNotFoundError):
            await _make_adapter().get_product("shopify:123")

    @pytest.mark.asyncio
    async def test_get_capabilities(self):
        caps = await _make_adapter().get_capabilities()
        assert caps["order_create"] is True
        assert caps["search"] is False
        assert caps["adapter"] == "shopify_ucp"


class TestCreateOrder:
    @pytest.mark.asyncio
    async def test_successful_order_three_phase(self, tmp_path):
        _write_shop_pay(tmp_path)
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)

        try:
            with respx.mock:
                respx.post(_AUTH_URL).mock(return_value=httpx.Response(200, json=_JWT_RESPONSE))
                respx.post(_MCP_URL).mock(
                    side_effect=[
                        httpx.Response(200, json=_rpc_response(_CREATE_RESPONSE)),   # create
                        httpx.Response(200, json=_rpc_response(_UPDATE_RESPONSE)),   # update
                        httpx.Response(200, json=_rpc_response(_COMPLETE_RESPONSE)), # complete
                    ]
                )

                result = await adapter.create_order(
                    sku=f"shop-cli-test-myshopify-com:62667067457907",
                    quantity=1,
                    mandate_id="m-123",
                    idempotency_key="idem-001",
                    checkout_url=f"https://{_STORE}/cart/62667067457907:1",
                )
        finally:
            os.environ.pop("SHOP_HOME", None)

        assert result["shopify_order_name"] == "#1001"
        assert result["status"] == "completed"
        assert result["store_domain"] == _STORE

    @pytest.mark.asyncio
    async def test_rpc_calls_use_correct_methods(self, tmp_path):
        """Verify create/update/complete are called in the right order with right methods."""
        _write_shop_pay(tmp_path)
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)
        captured_methods = []

        def capture(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            captured_methods.append(body["method"])
            responses = {
                "create_checkout": _CREATE_RESPONSE,
                "update_checkout": _UPDATE_RESPONSE,
                "complete_checkout": _COMPLETE_RESPONSE,
            }
            return httpx.Response(200, json=_rpc_response(responses[body["method"]]))

        try:
            with respx.mock:
                respx.post(_AUTH_URL).mock(return_value=httpx.Response(200, json=_JWT_RESPONSE))
                respx.post(_MCP_URL).mock(side_effect=capture)

                await adapter.create_order(
                    sku="shop-cli-test-myshopify-com:62667067457907",
                    quantity=1,
                    mandate_id="m-123",
                    idempotency_key="idem-002",
                    checkout_url=f"https://{_STORE}/cart/62667067457907:1",
                )
        finally:
            os.environ.pop("SHOP_HOME", None)

        assert captured_methods == ["create_checkout", "update_checkout", "complete_checkout"]

    @pytest.mark.asyncio
    async def test_shop_pay_token_in_update_and_complete(self, tmp_path):
        """Shop Pay token must appear in both update_checkout and complete_checkout."""
        _write_shop_pay(tmp_path, token="spay_tok_mytoken")
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)
        captured_params: list[dict] = []

        def capture(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            captured_params.append({"method": body["method"], "params": body["params"]})
            responses = {
                "create_checkout": _CREATE_RESPONSE,
                "update_checkout": _UPDATE_RESPONSE,
                "complete_checkout": _COMPLETE_RESPONSE,
            }
            return httpx.Response(200, json=_rpc_response(responses[body["method"]]))

        try:
            with respx.mock:
                respx.post(_AUTH_URL).mock(return_value=httpx.Response(200, json=_JWT_RESPONSE))
                respx.post(_MCP_URL).mock(side_effect=capture)

                await adapter.create_order(
                    sku="shop-cli-test-myshopify-com:62667067457907",
                    quantity=1,
                    mandate_id="m-123",
                    idempotency_key="idem-003",
                    checkout_url=f"https://{_STORE}/cart/62667067457907:1",
                )
        finally:
            os.environ.pop("SHOP_HOME", None)

        update_params = next(p["params"] for p in captured_params if p["method"] == "update_checkout")
        instruments = update_params["checkout"]["payment"]["instruments"]
        assert instruments[0]["credential"] == "spay_tok_mytoken"
        assert instruments[0]["type"] == "SHOP_PAY"

        complete_params = next(p["params"] for p in captured_params if p["method"] == "complete_checkout")
        assert complete_params["payment"]["credential"] == "spay_tok_mytoken"

    @pytest.mark.asyncio
    async def test_polls_on_complete_in_progress(self, tmp_path):
        """If complete_checkout returns complete_in_progress, adapter polls get_checkout."""
        _write_shop_pay(tmp_path)
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)
        in_progress = dict(_COMPLETE_RESPONSE, status="complete_in_progress")
        captured_methods = []

        def capture(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            captured_methods.append(body["method"])
            if body["method"] == "complete_checkout":
                return httpx.Response(200, json=_rpc_response(in_progress))
            if body["method"] == "get_checkout":
                return httpx.Response(200, json=_rpc_response(_COMPLETE_RESPONSE))
            return httpx.Response(200, json=_rpc_response(
                {"create_checkout": _CREATE_RESPONSE, "update_checkout": _UPDATE_RESPONSE}[body["method"]]
            ))

        try:
            with respx.mock:
                respx.post(_AUTH_URL).mock(return_value=httpx.Response(200, json=_JWT_RESPONSE))
                respx.post(_MCP_URL).mock(side_effect=capture)

                # Patch sleep to avoid actual waiting
                import shop.adapters.shopify_ucp as mod
                original_sleep = mod._async_sleep
                mod._async_sleep = lambda _: __import__("asyncio").sleep(0)

                try:
                    result = await adapter.create_order(
                        sku="shop-cli-test-myshopify-com:62667067457907",
                        quantity=1,
                        mandate_id="m-123",
                        idempotency_key="idem-004",
                        checkout_url=f"https://{_STORE}/cart/62667067457907:1",
                    )
                finally:
                    mod._async_sleep = original_sleep
        finally:
            os.environ.pop("SHOP_HOME", None)

        assert "get_checkout" in captured_methods
        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_requires_escalation_raises_checkout_not_supported(self, tmp_path):
        _write_shop_pay(tmp_path)
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)

        escalation = dict(_CREATE_RESPONSE, status="requires_escalation", continue_url="https://shop-cli-test.myshopify.com/checkout/abc")

        try:
            with respx.mock:
                respx.post(_AUTH_URL).mock(return_value=httpx.Response(200, json=_JWT_RESPONSE))
                respx.post(_MCP_URL).mock(
                    return_value=httpx.Response(200, json=_rpc_response(escalation))
                )

                with pytest.raises(CheckoutNotSupportedError):
                    await adapter.create_order(
                        sku="shop-cli-test-myshopify-com:62667067457907",
                        quantity=1,
                        mandate_id="m-123",
                        idempotency_key="idem-005",
                        checkout_url=f"https://{_STORE}/cart/62667067457907:1",
                    )
        finally:
            os.environ.pop("SHOP_HOME", None)

    @pytest.mark.asyncio
    async def test_missing_shop_pay_raises_adapter_error(self, tmp_path):
        # Empty payment.yaml — no shop_pay method
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)

        try:
            with respx.mock:
                respx.post(_AUTH_URL).mock(return_value=httpx.Response(200, json=_JWT_RESPONSE))
                with pytest.raises(AdapterError, match="Shop Pay"):
                    await adapter.create_order(
                        sku="shop-cli-test-myshopify-com:62667067457907",
                        quantity=1,
                        mandate_id="m-123",
                        idempotency_key="idem-006",
                        checkout_url=f"https://{_STORE}/cart/62667067457907:1",
                    )
        finally:
            os.environ.pop("SHOP_HOME", None)

    @pytest.mark.asyncio
    async def test_missing_checkout_url_raises_adapter_error(self, tmp_path):
        _write_shop_pay(tmp_path)
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)

        try:
            with respx.mock:
                respx.post(_AUTH_URL).mock(return_value=httpx.Response(200, json=_JWT_RESPONSE))
                with pytest.raises(AdapterError, match="variant ID"):
                    await adapter.create_order(
                        sku="shop-cli-test-myshopify-com:no-variant",
                        quantity=1,
                        mandate_id="m-123",
                        idempotency_key="idem-007",
                        checkout_url=None,
                    )
        finally:
            os.environ.pop("SHOP_HOME", None)

    @pytest.mark.asyncio
    async def test_rpc_error_response_raises_adapter_error(self, tmp_path):
        _write_shop_pay(tmp_path)
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)

        try:
            with respx.mock:
                respx.post(_AUTH_URL).mock(return_value=httpx.Response(200, json=_JWT_RESPONSE))
                respx.post(_MCP_URL).mock(
                    return_value=httpx.Response(200, json=_rpc_error(-32600, "Variant not found"))
                )

                with pytest.raises(AdapterError, match="Variant not found"):
                    await adapter.create_order(
                        sku="shop-cli-test-myshopify-com:62667067457907",
                        quantity=1,
                        mandate_id="m-123",
                        idempotency_key="idem-008",
                        checkout_url=f"https://{_STORE}/cart/62667067457907:1",
                    )
        finally:
            os.environ.pop("SHOP_HOME", None)

    @pytest.mark.asyncio
    async def test_jwt_cached_across_calls(self, tmp_path):
        """JWT should be fetched once and reused within TTL."""
        _write_shop_pay(tmp_path)
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)

        try:
            with respx.mock:
                auth_route = respx.post(_AUTH_URL).mock(
                    return_value=httpx.Response(200, json=_JWT_RESPONSE)
                )
                respx.post(_MCP_URL).mock(
                    side_effect=[
                        httpx.Response(200, json=_rpc_response(_CREATE_RESPONSE)),
                        httpx.Response(200, json=_rpc_response(_UPDATE_RESPONSE)),
                        httpx.Response(200, json=_rpc_response(_COMPLETE_RESPONSE)),
                    ]
                )

                await adapter.create_order(
                    sku="shop-cli-test-myshopify-com:62667067457907",
                    quantity=1,
                    mandate_id="m-123",
                    idempotency_key="idem-009",
                    checkout_url=f"https://{_STORE}/cart/62667067457907:1",
                )
                assert len(auth_route.calls) == 1  # only one auth call for three RPC calls
        finally:
            os.environ.pop("SHOP_HOME", None)
