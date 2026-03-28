"""Tests for ACPAdapter — ACP checkout flow, Stripe credential injection, error cases."""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
import respx
import yaml

from shop.adapters.acp import ACPAdapter, _load_stripe_credential
from shop.adapters.base import AdapterError, CheckoutNotSupportedError, ProductNotFoundError
from shop.models.commerce import SearchFilters

_ENDPOINT = "https://merchant.example.com/api/acp"
_SLUG = "merchant-example-com"
_ACP_KEY = "acp_test_key_abc123"


def _make_adapter(acp_key: str = _ACP_KEY) -> ACPAdapter:
    return ACPAdapter(
        slug=_SLUG,
        config={"acp_endpoint": _ENDPOINT, "acp_key": acp_key},
    )


def _confirmed_response(order_id: str = "ord_acp_001") -> dict:
    return {
        "order_id": order_id,
        "status": "confirmed",
        "total_cents": 2999,
        "currency": "USD",
        "confirmation_code": "ACP-CONF-001",
    }


def _write_stripe_payment(
    tmp_path: Path, customer_id: str = "cus_test", pm_id: str = "pm_test"
) -> None:
    data = {
        "default": pm_id,
        "methods": [
            {
                "id": pm_id,
                "label": "My Visa",
                "type": "stripe",
                "customer_id": customer_id,
                "payment_method_id": pm_id,
                "card_last4": "4242",
                "card_brand": "visa",
                "expiry": "12/2026",
            }
        ],
        "pending": [],
    }
    p = tmp_path / "payment.yaml"
    p.write_text(yaml.dump(data))
    p.chmod(0o600)


# ---------------------------------------------------------------------------
# Unsupported operations
# ---------------------------------------------------------------------------


class TestUnsupportedOperations:
    @pytest.mark.asyncio
    async def test_search_raises_adapter_error(self):
        adapter = _make_adapter()
        with pytest.raises(AdapterError):
            await adapter.search("coffee", SearchFilters())

    @pytest.mark.asyncio
    async def test_get_product_raises_product_not_found(self):
        adapter = _make_adapter()
        with pytest.raises(ProductNotFoundError):
            await adapter.get_product(f"{_SLUG}:sku-123")

    @pytest.mark.asyncio
    async def test_get_capabilities_returns_correct_shape(self):
        adapter = _make_adapter()
        caps = await adapter.get_capabilities()
        assert caps["adapter"] == "acp"
        assert caps["order_create"] is True
        assert caps["search"] is False
        assert caps["payment_handler"] == "stripe"


# ---------------------------------------------------------------------------
# Stripe credential loading
# ---------------------------------------------------------------------------


class TestLoadStripeCredential:
    def test_returns_credentials_when_stripe_method_present(self, tmp_path):
        _write_stripe_payment(tmp_path)
        result = _load_stripe_credential(tmp_path)
        assert result == {"customer_id": "cus_test", "payment_method_id": "pm_test"}

    def test_returns_none_when_no_payment_file(self, tmp_path):
        assert _load_stripe_credential(tmp_path) is None

    def test_returns_none_when_only_shop_pay_method(self, tmp_path):
        data = {
            "default": "sp_1",
            "methods": [{"id": "sp_1", "type": "shop_pay", "shop_pay_token": "tok_xyz"}],
        }
        (tmp_path / "payment.yaml").write_text(yaml.dump(data))
        assert _load_stripe_credential(tmp_path) is None

    def test_prefers_default_stripe_method(self, tmp_path):
        data = {
            "default": "pm_second",
            "methods": [
                {
                    "id": "pm_first",
                    "type": "stripe",
                    "customer_id": "cus_1",
                    "payment_method_id": "pm_first",
                },
                {
                    "id": "pm_second",
                    "type": "stripe",
                    "customer_id": "cus_2",
                    "payment_method_id": "pm_second",
                },
            ],
        }
        (tmp_path / "payment.yaml").write_text(yaml.dump(data))
        result = _load_stripe_credential(tmp_path)
        assert result["customer_id"] == "cus_2"


# ---------------------------------------------------------------------------
# Successful checkout
# ---------------------------------------------------------------------------


class TestCreateOrder:
    @pytest.mark.asyncio
    async def test_successful_checkout(self, tmp_path):
        _write_stripe_payment(tmp_path)
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)

        try:
            with respx.mock:
                respx.post(f"{_ENDPOINT}/checkout").mock(
                    return_value=httpx.Response(200, json=_confirmed_response())
                )
                result = await adapter.create_order(
                    sku=f"{_SLUG}:product-42",
                    quantity=1,
                    mandate_id="m-001",
                    idempotency_key="idem-001",
                )
        finally:
            os.environ.pop("SHOP_HOME", None)

        assert result["order_id"] == "ord_acp_001"
        assert result["status"] == "confirmed"
        assert result["adapter"] == "acp"

    @pytest.mark.asyncio
    async def test_request_body_contains_stripe_credentials(self, tmp_path):
        _write_stripe_payment(tmp_path, customer_id="cus_abc", pm_id="pm_abc")
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)
        captured: dict = {}

        def capture(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(200, json=_confirmed_response())

        try:
            with respx.mock:
                respx.post(f"{_ENDPOINT}/checkout").mock(side_effect=capture)
                await adapter.create_order(
                    sku=f"{_SLUG}:prod-1", quantity=2, mandate_id="m-1", idempotency_key="k-1"
                )
        finally:
            os.environ.pop("SHOP_HOME", None)

        assert captured["payment"]["type"] == "stripe"
        assert captured["payment"]["customer_id"] == "cus_abc"
        assert captured["payment"]["payment_method_id"] == "pm_abc"
        assert captured["items"] == [{"sku": "prod-1", "quantity": 2}]
        assert captured["mandate_id"] == "m-1"

    @pytest.mark.asyncio
    async def test_request_has_auth_and_idempotency_headers(self, tmp_path):
        _write_stripe_payment(tmp_path)
        adapter = _make_adapter(acp_key="my-key")
        os.environ["SHOP_HOME"] = str(tmp_path)
        captured_headers: dict = {}

        def capture(request: httpx.Request) -> httpx.Response:
            captured_headers.update(dict(request.headers))
            return httpx.Response(200, json=_confirmed_response())

        try:
            with respx.mock:
                respx.post(f"{_ENDPOINT}/checkout").mock(side_effect=capture)
                await adapter.create_order(
                    sku=f"{_SLUG}:item", quantity=1, mandate_id="m", idempotency_key="idem-hdr"
                )
        finally:
            os.environ.pop("SHOP_HOME", None)

        assert captured_headers.get("authorization") == "Bearer my-key"
        assert captured_headers.get("idempotency-key") == "idem-hdr"
        assert "request-id" in captured_headers

    @pytest.mark.asyncio
    async def test_sku_prefix_stripped(self, tmp_path):
        _write_stripe_payment(tmp_path)
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)
        captured: dict = {}

        def capture(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(200, json=_confirmed_response())

        try:
            with respx.mock:
                respx.post(f"{_ENDPOINT}/checkout").mock(side_effect=capture)
                await adapter.create_order(
                    sku=f"{_SLUG}:my-product", quantity=1, mandate_id="m", idempotency_key="k"
                )
        finally:
            os.environ.pop("SHOP_HOME", None)

        assert captured["items"][0]["sku"] == "my-product"  # slug prefix stripped

    @pytest.mark.asyncio
    async def test_409_returns_existing_order(self, tmp_path):
        _write_stripe_payment(tmp_path)
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)
        existing = {"order_id": "ord_prior", "status": "confirmed"}

        try:
            with respx.mock:
                respx.post(f"{_ENDPOINT}/checkout").mock(
                    return_value=httpx.Response(409, json=existing)
                )
                result = await adapter.create_order(
                    sku=f"{_SLUG}:item", quantity=1, mandate_id="m", idempotency_key="dup"
                )
        finally:
            os.environ.pop("SHOP_HOME", None)

        assert result["order_id"] == "ord_prior"


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


class TestErrorCases:
    @pytest.mark.asyncio
    async def test_no_acp_endpoint_raises(self, tmp_path):
        adapter = ACPAdapter(slug=_SLUG, config={})
        _write_stripe_payment(tmp_path)
        os.environ["SHOP_HOME"] = str(tmp_path)
        try:
            with pytest.raises(AdapterError, match="acp_endpoint"):
                await adapter.create_order("sku", 1, "m", "k")
        finally:
            os.environ.pop("SHOP_HOME", None)

    @pytest.mark.asyncio
    async def test_no_stripe_payment_raises(self, tmp_path):
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)
        try:
            with pytest.raises(AdapterError, match="Stripe"):
                await adapter.create_order(f"{_SLUG}:item", 1, "m", "k")
        finally:
            os.environ.pop("SHOP_HOME", None)

    @pytest.mark.asyncio
    async def test_501_raises_checkout_not_supported(self, tmp_path):
        _write_stripe_payment(tmp_path)
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)

        try:
            with respx.mock:
                respx.post(f"{_ENDPOINT}/checkout").mock(
                    return_value=httpx.Response(501, json={"error": "not implemented"})
                )
                with pytest.raises(CheckoutNotSupportedError):
                    await adapter.create_order(f"{_SLUG}:item", 1, "m", "k")
        finally:
            os.environ.pop("SHOP_HOME", None)

    @pytest.mark.asyncio
    async def test_401_raises_adapter_error_with_auth_message(self, tmp_path):
        _write_stripe_payment(tmp_path)
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)

        try:
            with respx.mock:
                respx.post(f"{_ENDPOINT}/checkout").mock(
                    return_value=httpx.Response(401, json={"error": "unauthorized"})
                )
                with pytest.raises(AdapterError, match="auth"):
                    await adapter.create_order(f"{_SLUG}:item", 1, "m", "k")
        finally:
            os.environ.pop("SHOP_HOME", None)

    @pytest.mark.asyncio
    async def test_requires_action_raises_checkout_not_supported(self, tmp_path):
        _write_stripe_payment(tmp_path)
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)

        try:
            with respx.mock:
                respx.post(f"{_ENDPOINT}/checkout").mock(
                    return_value=httpx.Response(
                        200,
                        json={
                            "order_id": "ord_pending",
                            "status": "requires_action",
                            "action_url": "https://merchant.example.com/verify",
                        },
                    )
                )
                with pytest.raises(CheckoutNotSupportedError):
                    await adapter.create_order(f"{_SLUG}:item", 1, "m", "k")
        finally:
            os.environ.pop("SHOP_HOME", None)

    @pytest.mark.asyncio
    async def test_declined_status_raises_adapter_error(self, tmp_path):
        _write_stripe_payment(tmp_path)
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)

        try:
            with respx.mock:
                respx.post(f"{_ENDPOINT}/checkout").mock(
                    return_value=httpx.Response(
                        200,
                        json={
                            "order_id": "ord_dec",
                            "status": "declined",
                            "message": "Insufficient funds",
                        },
                    )
                )
                with pytest.raises(AdapterError, match="declined"):
                    await adapter.create_order(f"{_SLUG}:item", 1, "m", "k")
        finally:
            os.environ.pop("SHOP_HOME", None)

    @pytest.mark.asyncio
    async def test_missing_order_id_raises_adapter_error(self, tmp_path):
        _write_stripe_payment(tmp_path)
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)

        try:
            with respx.mock:
                respx.post(f"{_ENDPOINT}/checkout").mock(
                    return_value=httpx.Response(200, json={"status": "confirmed"})  # no order_id
                )
                with pytest.raises(AdapterError, match="order_id"):
                    await adapter.create_order(f"{_SLUG}:item", 1, "m", "k")
        finally:
            os.environ.pop("SHOP_HOME", None)

    @pytest.mark.asyncio
    async def test_timeout_raises_timeout_error(self, tmp_path):
        _write_stripe_payment(tmp_path)
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)

        try:
            with respx.mock:
                respx.post(f"{_ENDPOINT}/checkout").mock(side_effect=httpx.TimeoutException(""))
                with pytest.raises(TimeoutError):
                    await adapter.create_order(f"{_SLUG}:item", 1, "m", "k")
        finally:
            os.environ.pop("SHOP_HOME", None)
