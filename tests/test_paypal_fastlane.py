"""Tests for PayPalFastlaneAdapter — auth, checkout flow, Fastlane token, error cases."""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
import respx
import yaml

from shop.adapters.base import AdapterError, CheckoutNotSupportedError, ProductNotFoundError
from shop.adapters.paypal_fastlane import PayPalFastlaneAdapter, _load_fastlane_credential
from shop.models.commerce import SearchFilters

_SANDBOX_BASE = "https://api-m.sandbox.paypal.com"
_LIVE_BASE = "https://api-m.paypal.com"
_SLUG = "pp-merchant"
_CLIENT_ID = "pp_test_client"
_CLIENT_SECRET = "pp_test_secret"
_ACCESS_TOKEN = "A21AAKFPV"
_FASTLANE_TOKEN = "fl_tok_test_abc123"


def _make_adapter(sandbox: bool = True) -> PayPalFastlaneAdapter:
    return PayPalFastlaneAdapter(
        slug=_SLUG,
        config={
            "paypal_client_id": _CLIENT_ID,
            "paypal_client_secret": _CLIENT_SECRET,
            "paypal_sandbox": "true" if sandbox else "false",
            "currency": "USD",
        },
    )


def _write_fastlane_payment(
    tmp_path: Path, token: str = _FASTLANE_TOKEN, email: str = "buyer@example.com"
) -> None:
    method_id = "ppfl_test01"
    data = {
        "default": method_id,
        "methods": [
            {
                "id": method_id,
                "label": "PayPal Fastlane",
                "type": "paypal_fastlane",
                "email": email,
                "fastlane_token": token,
                "billing_address": {"country": "US"},
            }
        ],
        "pending": [],
    }
    (tmp_path / "payment.yaml").write_text(yaml.dump(data))


def _token_response() -> dict:
    return {"access_token": _ACCESS_TOKEN, "token_type": "Bearer", "expires_in": 3600}


def _create_order_response(order_id: str = "PP-ORDER-001") -> dict:
    return {"id": order_id, "status": "APPROVED", "links": []}


def _capture_response(order_id: str = "PP-ORDER-001") -> dict:
    return {
        "id": order_id,
        "status": "COMPLETED",
        "purchase_units": [
            {
                "payments": {
                    "captures": [
                        {"id": "CAP-001", "status": "COMPLETED", "amount": {"value": "29.99"}}
                    ]
                }
            }
        ],
    }


# ---------------------------------------------------------------------------
# Unsupported operations
# ---------------------------------------------------------------------------


class TestUnsupportedOperations:
    @pytest.mark.asyncio
    async def test_search_raises_adapter_error(self):
        with pytest.raises(AdapterError):
            await _make_adapter().search("coffee", SearchFilters())

    @pytest.mark.asyncio
    async def test_get_product_raises_product_not_found(self):
        with pytest.raises(ProductNotFoundError):
            await _make_adapter().get_product("some-sku")

    @pytest.mark.asyncio
    async def test_get_capabilities_shape(self):
        caps = await _make_adapter().get_capabilities()
        assert caps["adapter"] == "paypal_fastlane"
        assert caps["order_create"] is True
        assert caps["search"] is False
        assert caps["payment_handler"] == "paypal_fastlane"


# ---------------------------------------------------------------------------
# Fastlane credential loading
# ---------------------------------------------------------------------------


class TestLoadFastlaneCredential:
    def test_returns_credential_when_present(self, tmp_path):
        _write_fastlane_payment(tmp_path, token="fl_tok_abc")
        cred = _load_fastlane_credential(tmp_path)
        assert cred is not None
        assert cred["token"] == "fl_tok_abc"
        assert cred["email"] == "buyer@example.com"

    def test_returns_none_when_no_file(self, tmp_path):
        assert _load_fastlane_credential(tmp_path) is None

    def test_returns_none_when_stripe_method_only(self, tmp_path):
        data = {
            "default": "pm_1",
            "methods": [
                {
                    "id": "pm_1",
                    "type": "stripe",
                    "customer_id": "cus_x",
                    "payment_method_id": "pm_x",
                }
            ],
        }
        (tmp_path / "payment.yaml").write_text(yaml.dump(data))
        assert _load_fastlane_credential(tmp_path) is None

    def test_prefers_default_fastlane_method(self, tmp_path):
        data = {
            "default": "ppfl_second",
            "methods": [
                {
                    "id": "ppfl_first",
                    "type": "paypal_fastlane",
                    "fastlane_token": "tok_first",
                    "email": "a@b.com",
                },
                {
                    "id": "ppfl_second",
                    "type": "paypal_fastlane",
                    "fastlane_token": "tok_second",
                    "email": "c@d.com",
                },
            ],
        }
        (tmp_path / "payment.yaml").write_text(yaml.dump(data))
        cred = _load_fastlane_credential(tmp_path)
        assert cred["token"] == "tok_second"


# ---------------------------------------------------------------------------
# OAuth token caching
# ---------------------------------------------------------------------------


class TestAccessTokenCaching:
    @pytest.mark.asyncio
    async def test_token_cached_after_first_call(self, tmp_path):
        """Second call to _get_access_token should not make a network request."""
        adapter = _make_adapter()
        call_count = 0

        def handle_auth(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(200, json=_token_response())

        with respx.mock:
            respx.post(f"{_SANDBOX_BASE}/v1/oauth2/token").mock(side_effect=handle_auth)
            await adapter._get_access_token()
            await adapter._get_access_token()

        assert call_count == 1

    @pytest.mark.asyncio
    async def test_invalid_credentials_raises_adapter_error(self):
        adapter = _make_adapter()
        with respx.mock:
            respx.post(f"{_SANDBOX_BASE}/v1/oauth2/token").mock(
                return_value=httpx.Response(401, json={"error": "invalid_client"})
            )
            with pytest.raises(AdapterError, match="client_id"):
                await adapter._get_access_token()


# ---------------------------------------------------------------------------
# Full checkout flow
# ---------------------------------------------------------------------------


class TestCreateOrder:
    @pytest.mark.asyncio
    async def test_successful_two_phase_checkout(self, tmp_path):
        _write_fastlane_payment(tmp_path)
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)

        try:
            with respx.mock:
                respx.post(f"{_SANDBOX_BASE}/v1/oauth2/token").mock(
                    return_value=httpx.Response(200, json=_token_response())
                )
                respx.post(f"{_SANDBOX_BASE}/v2/checkout/orders").mock(
                    return_value=httpx.Response(200, json=_create_order_response("PP-001"))
                )
                respx.post(f"{_SANDBOX_BASE}/v2/checkout/orders/PP-001/capture").mock(
                    return_value=httpx.Response(200, json=_capture_response("PP-001"))
                )
                result = await adapter.create_order(
                    sku=f"{_SLUG}:product-x",
                    quantity=2,
                    mandate_id="m-1",
                    idempotency_key="idem-1",
                )
        finally:
            os.environ.pop("SHOP_HOME", None)

        assert result["paypal_order_id"] == "PP-001"
        assert result["paypal_capture_id"] == "CAP-001"
        assert result["status"] == "completed"
        assert result["adapter"] == "paypal_fastlane"

    @pytest.mark.asyncio
    async def test_fastlane_token_in_create_body(self, tmp_path):
        _write_fastlane_payment(tmp_path, token="fl_tok_xyz")
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)
        captured: dict = {}

        def capture_create(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(200, json=_create_order_response())

        try:
            with respx.mock:
                respx.post(f"{_SANDBOX_BASE}/v1/oauth2/token").mock(
                    return_value=httpx.Response(200, json=_token_response())
                )
                respx.post(f"{_SANDBOX_BASE}/v2/checkout/orders").mock(side_effect=capture_create)
                respx.post(f"{_SANDBOX_BASE}/v2/checkout/orders/PP-ORDER-001/capture").mock(
                    return_value=httpx.Response(200, json=_capture_response())
                )
                await adapter.create_order(
                    sku=f"{_SLUG}:prod", quantity=1, mandate_id="m", idempotency_key="k"
                )
        finally:
            os.environ.pop("SHOP_HOME", None)

        payment_source = captured.get("payment_source", {})
        assert payment_source.get("token", {}).get("id") == "fl_tok_xyz"
        assert payment_source["token"]["type"] == "BILLING_AGREEMENT"

    @pytest.mark.asyncio
    async def test_sku_prefix_stripped_in_reference_id(self, tmp_path):
        _write_fastlane_payment(tmp_path)
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)
        captured: dict = {}

        def capture_create(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(200, json=_create_order_response())

        try:
            with respx.mock:
                respx.post(f"{_SANDBOX_BASE}/v1/oauth2/token").mock(
                    return_value=httpx.Response(200, json=_token_response())
                )
                respx.post(f"{_SANDBOX_BASE}/v2/checkout/orders").mock(side_effect=capture_create)
                respx.post(f"{_SANDBOX_BASE}/v2/checkout/orders/PP-ORDER-001/capture").mock(
                    return_value=httpx.Response(200, json=_capture_response())
                )
                await adapter.create_order(
                    sku=f"{_SLUG}:my-product", quantity=1, mandate_id="m", idempotency_key="k"
                )
        finally:
            os.environ.pop("SHOP_HOME", None)

        pu = captured["purchase_units"][0]
        assert pu["reference_id"] == "my-product"  # prefix stripped

    @pytest.mark.asyncio
    async def test_live_api_used_when_sandbox_false(self, tmp_path):
        _write_fastlane_payment(tmp_path)
        adapter = PayPalFastlaneAdapter(
            slug=_SLUG,
            config={
                "paypal_client_id": _CLIENT_ID,
                "paypal_client_secret": _CLIENT_SECRET,
                "paypal_sandbox": "false",
                "currency": "USD",
            },
        )
        os.environ["SHOP_HOME"] = str(tmp_path)

        try:
            with respx.mock:
                auth_route = respx.post(f"{_LIVE_BASE}/v1/oauth2/token").mock(
                    return_value=httpx.Response(200, json=_token_response())
                )
                respx.post(f"{_LIVE_BASE}/v2/checkout/orders").mock(
                    return_value=httpx.Response(200, json=_create_order_response("PP-LIVE-001"))
                )
                respx.post(f"{_LIVE_BASE}/v2/checkout/orders/PP-LIVE-001/capture").mock(
                    return_value=httpx.Response(200, json=_capture_response("PP-LIVE-001"))
                )
                await adapter.create_order(
                    sku=f"{_SLUG}:item", quantity=1, mandate_id="m", idempotency_key="k"
                )
        finally:
            os.environ.pop("SHOP_HOME", None)

        assert auth_route.called  # confirms live base was used


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


class TestErrorCases:
    @pytest.mark.asyncio
    async def test_missing_credentials_raises(self, tmp_path):
        adapter = PayPalFastlaneAdapter(slug=_SLUG, config={})
        _write_fastlane_payment(tmp_path)
        os.environ["SHOP_HOME"] = str(tmp_path)
        try:
            with pytest.raises(AdapterError, match="paypal_client_id"):
                await adapter.create_order("sku", 1, "m", "k")
        finally:
            os.environ.pop("SHOP_HOME", None)

    @pytest.mark.asyncio
    async def test_no_fastlane_token_raises(self, tmp_path):
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)
        try:
            with pytest.raises(AdapterError, match="Fastlane"):
                await adapter.create_order("sku", 1, "m", "k")
        finally:
            os.environ.pop("SHOP_HOME", None)

    @pytest.mark.asyncio
    async def test_payer_action_required_raises_checkout_not_supported(self, tmp_path):
        _write_fastlane_payment(tmp_path)
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)

        try:
            with respx.mock:
                respx.post(f"{_SANDBOX_BASE}/v1/oauth2/token").mock(
                    return_value=httpx.Response(200, json=_token_response())
                )
                respx.post(f"{_SANDBOX_BASE}/v2/checkout/orders").mock(
                    return_value=httpx.Response(
                        200,
                        json={
                            "id": "PP-PENDING",
                            "status": "PAYER_ACTION_REQUIRED",
                            "links": [
                                {"rel": "payer-action", "href": "https://paypal.com/auth/xxx"}
                            ],
                        },
                    )
                )
                with pytest.raises(CheckoutNotSupportedError, match="buyer action"):
                    await adapter.create_order(f"{_SLUG}:item", 1, "m", "k")
        finally:
            os.environ.pop("SHOP_HOME", None)

    @pytest.mark.asyncio
    async def test_missing_order_id_raises_adapter_error(self, tmp_path):
        _write_fastlane_payment(tmp_path)
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)

        try:
            with respx.mock:
                respx.post(f"{_SANDBOX_BASE}/v1/oauth2/token").mock(
                    return_value=httpx.Response(200, json=_token_response())
                )
                respx.post(f"{_SANDBOX_BASE}/v2/checkout/orders").mock(
                    return_value=httpx.Response(200, json={"status": "CREATED"})  # no id
                )
                with pytest.raises(AdapterError, match="order ID"):
                    await adapter.create_order(f"{_SLUG}:item", 1, "m", "k")
        finally:
            os.environ.pop("SHOP_HOME", None)

    @pytest.mark.asyncio
    async def test_capture_not_completed_raises_adapter_error(self, tmp_path):
        _write_fastlane_payment(tmp_path)
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)

        try:
            with respx.mock:
                respx.post(f"{_SANDBOX_BASE}/v1/oauth2/token").mock(
                    return_value=httpx.Response(200, json=_token_response())
                )
                respx.post(f"{_SANDBOX_BASE}/v2/checkout/orders").mock(
                    return_value=httpx.Response(200, json=_create_order_response("PP-FAIL"))
                )
                respx.post(f"{_SANDBOX_BASE}/v2/checkout/orders/PP-FAIL/capture").mock(
                    return_value=httpx.Response(
                        200,
                        json={
                            "id": "PP-FAIL",
                            "status": "DECLINED",
                            "purchase_units": [
                                {"payments": {"captures": [{"id": "cap", "status": "DECLINED"}]}}
                            ],
                        },
                    )
                )
                with pytest.raises(AdapterError, match="capture"):
                    await adapter.create_order(f"{_SLUG}:item", 1, "m", "k")
        finally:
            os.environ.pop("SHOP_HOME", None)

    @pytest.mark.asyncio
    async def test_timeout_raises_timeout_error(self, tmp_path):
        _write_fastlane_payment(tmp_path)
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)

        try:
            with respx.mock:
                respx.post(f"{_SANDBOX_BASE}/v1/oauth2/token").mock(
                    side_effect=httpx.TimeoutException("")
                )
                with pytest.raises(TimeoutError):
                    await adapter.create_order(f"{_SLUG}:item", 1, "m", "k")
        finally:
            os.environ.pop("SHOP_HOME", None)
