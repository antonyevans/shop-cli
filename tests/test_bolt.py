"""Tests for BoltAdapter — checkout flow, Bolt token, error cases."""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
import respx
import yaml

from shop.adapters.base import AdapterError, CheckoutNotSupportedError, ProductNotFoundError
from shop.adapters.bolt import BoltAdapter, _load_bolt_credential
from shop.models.commerce import SearchFilters

_SANDBOX_BASE = "https://api-sandbox.bolt.com"
_LIVE_BASE = "https://api.bolt.com"
_SLUG = "bolt-merchant"
_API_KEY = "bolt_test_api_key"
_MERCHANT_ID = "bolt_merchant_abc"
_BOLT_TOKEN = "bolt_tok_test_xyz"


def _make_adapter(sandbox: bool = True) -> BoltAdapter:
    return BoltAdapter(
        slug=_SLUG,
        config={
            "bolt_api_key": _API_KEY,
            "bolt_merchant_id": _MERCHANT_ID,
            "bolt_sandbox": "true" if sandbox else "false",
            "currency": "USD",
        },
    )


def _write_bolt_payment(
    tmp_path: Path, token: str = _BOLT_TOKEN, email: str = "buyer@example.com"
) -> None:
    method_id = "bolt_test01"
    data = {
        "default": method_id,
        "methods": [{
            "id": method_id,
            "label": "Bolt",
            "type": "bolt",
            "email": email,
            "bolt_token": token,
            "billing_address": {"country_code": "US"},
        }],
        "pending": [],
    }
    (tmp_path / "payment.yaml").write_text(yaml.dump(data))


def _success_response(reference: str = "BOLT-REF-001") -> dict:
    return {
        "transaction": {
            "reference": reference,
            "id": "txn_bolt_abc",
            "status": "completed",
        }
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
        assert caps["adapter"] == "bolt"
        assert caps["order_create"] is True
        assert caps["search"] is False
        assert caps["payment_handler"] == "bolt"


# ---------------------------------------------------------------------------
# Bolt credential loading
# ---------------------------------------------------------------------------

class TestLoadBoltCredential:
    def test_returns_credential_when_present(self, tmp_path):
        _write_bolt_payment(tmp_path, token="bolt_tok_abc")
        cred = _load_bolt_credential(tmp_path)
        assert cred is not None
        assert cred["token"] == "bolt_tok_abc"
        assert cred["email"] == "buyer@example.com"

    def test_returns_none_when_no_file(self, tmp_path):
        assert _load_bolt_credential(tmp_path) is None

    def test_returns_none_when_only_stripe_method(self, tmp_path):
        data = {
            "default": "pm_1",
            "methods": [{"id": "pm_1", "type": "stripe", "customer_id": "cus_x", "payment_method_id": "pm_x"}],
        }
        (tmp_path / "payment.yaml").write_text(yaml.dump(data))
        assert _load_bolt_credential(tmp_path) is None

    def test_prefers_default_bolt_method(self, tmp_path):
        data = {
            "default": "bolt_second",
            "methods": [
                {"id": "bolt_first", "type": "bolt", "bolt_token": "tok_first"},
                {"id": "bolt_second", "type": "bolt", "bolt_token": "tok_second"},
            ],
        }
        (tmp_path / "payment.yaml").write_text(yaml.dump(data))
        cred = _load_bolt_credential(tmp_path)
        assert cred["token"] == "tok_second"


# ---------------------------------------------------------------------------
# Successful checkout
# ---------------------------------------------------------------------------

class TestCreateOrder:
    @pytest.mark.asyncio
    async def test_successful_checkout(self, tmp_path):
        _write_bolt_payment(tmp_path)
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)

        try:
            with respx.mock:
                respx.post(f"{_SANDBOX_BASE}/v1/account/checkout").mock(
                    return_value=httpx.Response(200, json=_success_response("BOLT-001"))
                )
                result = await adapter.create_order(
                    sku=f"{_SLUG}:product-x", quantity=1,
                    mandate_id="m-1", idempotency_key="idem-1",
                )
        finally:
            os.environ.pop("SHOP_HOME", None)

        assert result["bolt_order_reference"] == "BOLT-001"
        assert result["bolt_transaction_id"] == "txn_bolt_abc"
        assert result["status"] == "completed"
        assert result["adapter"] == "bolt"

    @pytest.mark.asyncio
    async def test_bolt_token_in_request_body(self, tmp_path):
        _write_bolt_payment(tmp_path, token="bolt_tok_xyz")
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)
        captured: dict = {}

        def capture(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(200, json=_success_response())

        try:
            with respx.mock:
                respx.post(f"{_SANDBOX_BASE}/v1/account/checkout").mock(side_effect=capture)
                await adapter.create_order(
                    sku=f"{_SLUG}:prod", quantity=2, mandate_id="m", idempotency_key="k"
                )
        finally:
            os.environ.pop("SHOP_HOME", None)

        assert captured["payment"]["token"] == "bolt_tok_xyz"

    @pytest.mark.asyncio
    async def test_auth_headers_sent(self, tmp_path):
        _write_bolt_payment(tmp_path)
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)
        captured_headers: dict = {}

        def capture(request: httpx.Request) -> httpx.Response:
            captured_headers.update(dict(request.headers))
            return httpx.Response(200, json=_success_response())

        try:
            with respx.mock:
                respx.post(f"{_SANDBOX_BASE}/v1/account/checkout").mock(side_effect=capture)
                await adapter.create_order(
                    sku=f"{_SLUG}:item", quantity=1, mandate_id="m", idempotency_key="idem-hdr"
                )
        finally:
            os.environ.pop("SHOP_HOME", None)

        assert captured_headers.get("authorization") == f"api-key {_API_KEY}"
        assert captured_headers.get("x-bolt-merchant-id") == _MERCHANT_ID
        assert "idempotency-key" in captured_headers

    @pytest.mark.asyncio
    async def test_sku_prefix_stripped(self, tmp_path):
        _write_bolt_payment(tmp_path)
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)
        captured: dict = {}

        def capture(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(200, json=_success_response())

        try:
            with respx.mock:
                respx.post(f"{_SANDBOX_BASE}/v1/account/checkout").mock(side_effect=capture)
                await adapter.create_order(
                    sku=f"{_SLUG}:my-product", quantity=1, mandate_id="m", idempotency_key="k"
                )
        finally:
            os.environ.pop("SHOP_HOME", None)

        item = captured["cart"]["items"][0]
        assert item["reference"] == "my-product"

    @pytest.mark.asyncio
    async def test_live_api_used_when_not_sandbox(self, tmp_path):
        _write_bolt_payment(tmp_path)
        adapter = BoltAdapter(
            slug=_SLUG,
            config={
                "bolt_api_key": _API_KEY,
                "bolt_merchant_id": _MERCHANT_ID,
                "bolt_sandbox": "false",
                "currency": "USD",
            },
        )
        os.environ["SHOP_HOME"] = str(tmp_path)

        try:
            with respx.mock:
                live_route = respx.post(f"{_LIVE_BASE}/v1/account/checkout").mock(
                    return_value=httpx.Response(200, json=_success_response())
                )
                await adapter.create_order(
                    sku=f"{_SLUG}:item", quantity=1, mandate_id="m", idempotency_key="k"
                )
        finally:
            os.environ.pop("SHOP_HOME", None)

        assert live_route.called


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

class TestErrorCases:
    @pytest.mark.asyncio
    async def test_missing_api_key_raises(self, tmp_path):
        adapter = BoltAdapter(slug=_SLUG, config={"bolt_merchant_id": _MERCHANT_ID})
        _write_bolt_payment(tmp_path)
        os.environ["SHOP_HOME"] = str(tmp_path)
        try:
            with pytest.raises(AdapterError, match="bolt_api_key"):
                await adapter.create_order("sku", 1, "m", "k")
        finally:
            os.environ.pop("SHOP_HOME", None)

    @pytest.mark.asyncio
    async def test_no_bolt_token_raises(self, tmp_path):
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)
        try:
            with pytest.raises(AdapterError, match="Bolt"):
                await adapter.create_order("sku", 1, "m", "k")
        finally:
            os.environ.pop("SHOP_HOME", None)

    @pytest.mark.asyncio
    async def test_401_raises_adapter_error(self, tmp_path):
        _write_bolt_payment(tmp_path)
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)

        try:
            with respx.mock:
                respx.post(f"{_SANDBOX_BASE}/v1/account/checkout").mock(
                    return_value=httpx.Response(401)
                )
                with pytest.raises(AdapterError, match="auth"):
                    await adapter.create_order(f"{_SLUG}:item", 1, "m", "k")
        finally:
            os.environ.pop("SHOP_HOME", None)

    @pytest.mark.asyncio
    async def test_503_raises_checkout_not_supported(self, tmp_path):
        _write_bolt_payment(tmp_path)
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)

        try:
            with respx.mock:
                respx.post(f"{_SANDBOX_BASE}/v1/account/checkout").mock(
                    return_value=httpx.Response(503)
                )
                with pytest.raises(CheckoutNotSupportedError):
                    await adapter.create_order(f"{_SLUG}:item", 1, "m", "k")
        finally:
            os.environ.pop("SHOP_HOME", None)

    @pytest.mark.asyncio
    async def test_pending_review_raises_checkout_not_supported(self, tmp_path):
        _write_bolt_payment(tmp_path)
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)

        try:
            with respx.mock:
                respx.post(f"{_SANDBOX_BASE}/v1/account/checkout").mock(
                    return_value=httpx.Response(200, json={
                        "transaction": {"reference": "BOLT-HOLD", "status": "pending_review"}
                    })
                )
                with pytest.raises(CheckoutNotSupportedError, match="review"):
                    await adapter.create_order(f"{_SLUG}:item", 1, "m", "k")
        finally:
            os.environ.pop("SHOP_HOME", None)

    @pytest.mark.asyncio
    async def test_failed_status_raises_adapter_error(self, tmp_path):
        _write_bolt_payment(tmp_path)
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)

        try:
            with respx.mock:
                respx.post(f"{_SANDBOX_BASE}/v1/account/checkout").mock(
                    return_value=httpx.Response(200, json={
                        "transaction": {"reference": "BOLT-FAIL", "status": "failed", "message": "Declined"}
                    })
                )
                with pytest.raises(AdapterError, match="failed"):
                    await adapter.create_order(f"{_SLUG}:item", 1, "m", "k")
        finally:
            os.environ.pop("SHOP_HOME", None)

    @pytest.mark.asyncio
    async def test_missing_reference_raises_adapter_error(self, tmp_path):
        _write_bolt_payment(tmp_path)
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)

        try:
            with respx.mock:
                respx.post(f"{_SANDBOX_BASE}/v1/account/checkout").mock(
                    return_value=httpx.Response(200, json={"transaction": {"status": "completed"}})  # no reference
                )
                with pytest.raises(AdapterError, match="reference"):
                    await adapter.create_order(f"{_SLUG}:item", 1, "m", "k")
        finally:
            os.environ.pop("SHOP_HOME", None)

    @pytest.mark.asyncio
    async def test_timeout_raises_timeout_error(self, tmp_path):
        _write_bolt_payment(tmp_path)
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)

        try:
            with respx.mock:
                respx.post(f"{_SANDBOX_BASE}/v1/account/checkout").mock(
                    side_effect=httpx.TimeoutException("")
                )
                with pytest.raises(TimeoutError):
                    await adapter.create_order(f"{_SLUG}:item", 1, "m", "k")
        finally:
            os.environ.pop("SHOP_HOME", None)
