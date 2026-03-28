"""Tests for UCPAdapter — checkout flow, signing, and Stripe credential injection."""

from __future__ import annotations

import json
import os

import httpx
import pytest
import respx
import yaml

from shop.adapters.base import AdapterError, CheckoutNotSupportedError, ProductNotFoundError
from shop.adapters.ucp import UCPAdapter, _load_stripe_payment
from shop.models.commerce import SearchFilters

_ENDPOINT = "https://merchant.example.com/ucp"
_SLUG = "example-merchant"


def _make_adapter() -> UCPAdapter:
    return UCPAdapter(slug=_SLUG, config={"ucp_endpoint": _ENDPOINT})


def _session_response(session_id: str = "sess_abc") -> dict:
    return {"id": session_id, "status": "created", "total": "19.99"}


def _order_response() -> dict:
    return {"order_id": "ord_xyz", "status": "confirmed", "total": "19.99"}


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


class TestCreateOrder:
    @pytest.mark.asyncio
    async def test_successful_order_two_phase(self, tmp_path):
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)

        try:
            with respx.mock:
                respx.post(f"{_ENDPOINT}/checkout-sessions").mock(
                    return_value=httpx.Response(200, json=_session_response("sess_1"))
                )
                respx.post(f"{_ENDPOINT}/checkout-sessions/sess_1/complete").mock(
                    return_value=httpx.Response(200, json=_order_response())
                )

                result = await adapter.create_order(
                    sku=f"{_SLUG}:widget-42",
                    quantity=2,
                    mandate_id="m-001",
                    idempotency_key="idem-001",
                )
        finally:
            os.environ.pop("SHOP_HOME", None)

        assert result["order_id"] == "ord_xyz"
        assert result["status"] == "confirmed"

    @pytest.mark.asyncio
    async def test_session_body_contains_sku_and_mandate(self, tmp_path):
        """Verify the correct fields are sent in the checkout-sessions POST body."""
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)
        captured_body: dict = {}

        def capture(request: httpx.Request) -> httpx.Response:
            captured_body.update(json.loads(request.content))
            return httpx.Response(200, json=_session_response())

        try:
            with respx.mock:
                respx.post(f"{_ENDPOINT}/checkout-sessions").mock(side_effect=capture)
                respx.post(f"{_ENDPOINT}/checkout-sessions/sess_abc/complete").mock(
                    return_value=httpx.Response(200, json=_order_response())
                )

                await adapter.create_order(
                    sku=f"{_SLUG}:widget-42",
                    quantity=3,
                    mandate_id="m-abc",
                    idempotency_key="idem-xyz",
                )
        finally:
            os.environ.pop("SHOP_HOME", None)

        assert captured_body["items"] == [{"sku": "widget-42", "quantity": 3}]
        assert captured_body["mandate_id"] == "m-abc"

    @pytest.mark.asyncio
    async def test_idempotency_conflict_returns_existing(self, tmp_path):
        adapter = _make_adapter()
        existing = {"id": "sess_existing", "status": "completed", "order_id": "ord_prior"}
        os.environ["SHOP_HOME"] = str(tmp_path)

        try:
            with respx.mock:
                respx.post(f"{_ENDPOINT}/checkout-sessions").mock(
                    return_value=httpx.Response(409, json=existing)
                )

                result = await adapter.create_order(
                    sku=f"{_SLUG}:item", quantity=1, mandate_id="m-1", idempotency_key="idem-dup"
                )
        finally:
            os.environ.pop("SHOP_HOME", None)

        assert result["order_id"] == "ord_prior"

    @pytest.mark.asyncio
    async def test_501_raises_checkout_not_supported(self, tmp_path):
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)

        try:
            with respx.mock:
                respx.post(f"{_ENDPOINT}/checkout-sessions").mock(
                    return_value=httpx.Response(501, json={"error": "not implemented"})
                )
                with pytest.raises(CheckoutNotSupportedError):
                    await adapter.create_order(
                        sku=f"{_SLUG}:item", quantity=1, mandate_id="m", idempotency_key="k"
                    )
        finally:
            os.environ.pop("SHOP_HOME", None)

    @pytest.mark.asyncio
    async def test_missing_session_id_raises_adapter_error(self, tmp_path):
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)

        try:
            with respx.mock:
                respx.post(f"{_ENDPOINT}/checkout-sessions").mock(
                    return_value=httpx.Response(200, json={"status": "created"})  # no id field
                )
                with pytest.raises(AdapterError, match="session_id"):
                    await adapter.create_order(
                        sku=f"{_SLUG}:item", quantity=1, mandate_id="m", idempotency_key="k"
                    )
        finally:
            os.environ.pop("SHOP_HOME", None)

    @pytest.mark.asyncio
    async def test_no_ucp_endpoint_raises(self):
        adapter = UCPAdapter(slug=_SLUG, config={})
        with pytest.raises(AdapterError, match="ucp_endpoint"):
            await adapter.create_order("sku", 1, "m", "k")

    @pytest.mark.asyncio
    async def test_request_has_required_headers(self, tmp_path):
        """UCP requires Request-Id, Idempotency-Key, Request-Signature headers."""
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)
        captured_headers: dict = {}

        def capture(request: httpx.Request) -> httpx.Response:
            captured_headers.update(dict(request.headers))
            return httpx.Response(200, json=_session_response())

        try:
            with respx.mock:
                respx.post(f"{_ENDPOINT}/checkout-sessions").mock(side_effect=capture)
                respx.post(f"{_ENDPOINT}/checkout-sessions/sess_abc/complete").mock(
                    return_value=httpx.Response(200, json=_order_response())
                )

                await adapter.create_order(
                    sku=f"{_SLUG}:item", quantity=1, mandate_id="m", idempotency_key="idem-hdr"
                )
        finally:
            os.environ.pop("SHOP_HOME", None)

        assert "request-id" in captured_headers
        assert captured_headers["idempotency-key"] == "idem-hdr"
        assert "request-signature" in captured_headers
        # ES256 detached payload: base64url(header)..base64url(sig)
        sig = captured_headers["request-signature"]
        assert ".." in sig


class TestStripeCredentialInjection:
    def _write_stripe_payment(self, tmp_path, customer_id="cus_abc", pm_id="pm_abc"):
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

    def test_load_stripe_payment_returns_credentials(self, tmp_path):
        self._write_stripe_payment(tmp_path)
        result = _load_stripe_payment(tmp_path)
        assert result == {
            "stripe_customer_id": "cus_abc",
            "stripe_payment_method_id": "pm_abc",
        }

    def test_load_stripe_payment_no_file_returns_none(self, tmp_path):
        assert _load_stripe_payment(tmp_path) is None

    def test_load_stripe_payment_non_stripe_type_returns_none(self, tmp_path):
        # Raw card format (old) — not a Stripe method
        data = {
            "default": "card_1",
            "methods": [
                {
                    "id": "card_1",
                    "label": "Dev Card",
                    "type": "credit_card",
                    "number": "4242424242424242",
                    "month": 12,
                    "year": 2026,
                    "cvv": "123",
                }
            ],
        }
        (tmp_path / "payment.yaml").write_text(yaml.dump(data))
        assert _load_stripe_payment(tmp_path) is None

    @pytest.mark.asyncio
    async def test_stripe_credentials_included_in_session_body(self, tmp_path):
        """If Stripe payment method is configured, it's included in the session payload."""
        self._write_stripe_payment(tmp_path, customer_id="cus_abc", pm_id="pm_abc")
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)
        captured_body: dict = {}

        def capture(request: httpx.Request) -> httpx.Response:
            captured_body.update(json.loads(request.content))
            return httpx.Response(200, json=_session_response())

        try:
            with respx.mock:
                respx.post(f"{_ENDPOINT}/checkout-sessions").mock(side_effect=capture)
                respx.post(f"{_ENDPOINT}/checkout-sessions/sess_abc/complete").mock(
                    return_value=httpx.Response(200, json=_order_response())
                )

                await adapter.create_order(
                    sku=f"{_SLUG}:item", quantity=1, mandate_id="m", idempotency_key="k"
                )
        finally:
            os.environ.pop("SHOP_HOME", None)

        assert "payment" in captured_body
        assert captured_body["payment"]["stripe_customer_id"] == "cus_abc"
        assert captured_body["payment"]["stripe_payment_method_id"] == "pm_abc"

    @pytest.mark.asyncio
    async def test_no_payment_config_omits_payment_field(self, tmp_path):
        """If no payment.yaml, the payment field is absent from session body."""
        adapter = _make_adapter()
        os.environ["SHOP_HOME"] = str(tmp_path)
        captured_body: dict = {}

        def capture(request: httpx.Request) -> httpx.Response:
            captured_body.update(json.loads(request.content))
            return httpx.Response(200, json=_session_response())

        try:
            with respx.mock:
                respx.post(f"{_ENDPOINT}/checkout-sessions").mock(side_effect=capture)
                respx.post(f"{_ENDPOINT}/checkout-sessions/sess_abc/complete").mock(
                    return_value=httpx.Response(200, json=_order_response())
                )

                await adapter.create_order(
                    sku=f"{_SLUG}:item", quantity=1, mandate_id="m", idempotency_key="k"
                )
        finally:
            os.environ.pop("SHOP_HOME", None)

        assert "payment" not in captured_body
