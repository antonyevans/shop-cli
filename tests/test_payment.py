"""Tests for shop payment add/confirm/list commands (Stripe Setup Intent flow)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
import yaml

from shop.commands.payment import (
    run_payment_add_command,
    run_payment_confirm_command,
    run_payment_list_command,
    run_payment_remove_command,
)

_STRIPE_API = "https://api.stripe.com/v1"
_STRIPE_KEY = "sk_test_fake"

_CUSTOMER_RESPONSE = {"id": "cus_test123", "object": "customer"}
_SESSION_RESPONSE = {
    "id": "cs_test_abc",
    "object": "checkout.session",
    "status": "open",
    "url": "https://checkout.stripe.com/c/pay/cs_test_abc",
    "setup_intent": None,
    "customer": "cus_test123",
}
_SESSION_COMPLETE = {
    "id": "cs_test_abc",
    "object": "checkout.session",
    "status": "complete",
    "setup_intent": "seti_test_xyz",
    "customer": "cus_test123",
}
_SETUP_INTENT_RESPONSE = {
    "id": "seti_test_xyz",
    "object": "setup_intent",
    "payment_method": "pm_test_visa",
    "status": "succeeded",
}
_PAYMENT_METHOD_RESPONSE = {
    "id": "pm_test_visa",
    "object": "payment_method",
    "card": {
        "brand": "visa",
        "last4": "4242",
        "exp_month": 12,
        "exp_year": 2026,
    },
}


class TestPaymentAdd:
    def test_add_returns_pending_with_setup_url(self, tmp_path, capsys):
        with respx.mock:
            respx.post(f"{_STRIPE_API}/customers").mock(
                return_value=httpx.Response(200, json=_CUSTOMER_RESPONSE)
            )
            respx.post(f"{_STRIPE_API}/checkout/sessions").mock(
                return_value=httpx.Response(200, json=_SESSION_RESPONSE)
            )

            with pytest.raises(SystemExit) as exc_info:
                run_payment_add_command(
                    label="My Visa",
                    stripe_key=_STRIPE_KEY,
                    shop_dir=tmp_path,
                )

        assert exc_info.value.code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["status"] == "pending"
        assert data["session_id"] == "cs_test_abc"
        assert "checkout.stripe.com" in data["setup_url"]
        assert data["expires_in"] > 0

    def test_add_saves_pending_to_yaml(self, tmp_path, capsys):
        with respx.mock:
            respx.post(f"{_STRIPE_API}/customers").mock(
                return_value=httpx.Response(200, json=_CUSTOMER_RESPONSE)
            )
            respx.post(f"{_STRIPE_API}/checkout/sessions").mock(
                return_value=httpx.Response(200, json=_SESSION_RESPONSE)
            )

            with pytest.raises(SystemExit):
                run_payment_add_command(label="My Visa", stripe_key=_STRIPE_KEY, shop_dir=tmp_path)
            capsys.readouterr()

        p = tmp_path / "payment.yaml"
        assert p.exists()
        assert oct(p.stat().st_mode)[-3:] == "600"

        saved = yaml.safe_load(p.read_text())
        assert len(saved["pending"]) == 1
        assert saved["pending"][0]["session_id"] == "cs_test_abc"
        assert saved["pending"][0]["label"] == "My Visa"
        assert saved["pending"][0]["customer_id"] == "cus_test123"

    def test_add_missing_stripe_key_exits_1(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as exc_info:
            run_payment_add_command(label="Card", stripe_key="", shop_dir=tmp_path)
        assert exc_info.value.code == 1
        data = json.loads(capsys.readouterr().out)
        assert data["error_code"] == "missing_stripe_key"

    def test_add_stripe_api_error_exits_2(self, tmp_path, capsys):
        with respx.mock:
            respx.post(f"{_STRIPE_API}/customers").mock(
                return_value=httpx.Response(401, json={"error": {"message": "Invalid API key"}})
            )

            with pytest.raises(SystemExit) as exc_info:
                run_payment_add_command(label="Card", stripe_key="sk_bad", shop_dir=tmp_path)

        assert exc_info.value.code == 2
        data = json.loads(capsys.readouterr().out)
        assert data["error_code"] == "stripe_error"
        assert "Invalid API key" in data["detail"]

    def test_add_duplicate_label_replaces_pending(self, tmp_path, capsys):
        """Adding same label twice replaces the pending entry."""
        session_2 = dict(_SESSION_RESPONSE, id="cs_test_second")

        with respx.mock:
            respx.post(f"{_STRIPE_API}/customers").mock(
                return_value=httpx.Response(200, json=_CUSTOMER_RESPONSE)
            )
            # First call returns original session, second call returns session_2
            respx.post(f"{_STRIPE_API}/checkout/sessions").mock(
                side_effect=[
                    httpx.Response(200, json=_SESSION_RESPONSE),
                    httpx.Response(200, json=session_2),
                ]
            )

            for _ in range(2):
                with pytest.raises(SystemExit):
                    run_payment_add_command(
                        label="My Visa", stripe_key=_STRIPE_KEY, shop_dir=tmp_path
                    )
                capsys.readouterr()

        saved = yaml.safe_load((tmp_path / "payment.yaml").read_text())
        assert len(saved["pending"]) == 1
        assert saved["pending"][0]["session_id"] == "cs_test_second"


class TestPaymentConfirm:
    def _write_pending(self, tmp_path):
        """Write a pending session to payment.yaml."""
        data = {
            "default": None,
            "methods": [],
            "pending": [
                {
                    "label": "My Visa",
                    "session_id": "cs_test_abc",
                    "customer_id": "cus_test123",
                    "email": "",
                }
            ],
        }
        p = tmp_path / "payment.yaml"
        p.write_text(yaml.dump(data))
        p.chmod(0o600)

    def test_confirm_polls_and_saves_method(self, tmp_path, capsys):
        self._write_pending(tmp_path)

        with respx.mock:
            respx.get(f"{_STRIPE_API}/checkout/sessions/cs_test_abc").mock(
                return_value=httpx.Response(200, json=_SESSION_COMPLETE)
            )
            respx.get(f"{_STRIPE_API}/setup_intents/seti_test_xyz").mock(
                return_value=httpx.Response(200, json=_SETUP_INTENT_RESPONSE)
            )
            respx.get(f"{_STRIPE_API}/payment_methods/pm_test_visa").mock(
                return_value=httpx.Response(200, json=_PAYMENT_METHOD_RESPONSE)
            )

            with pytest.raises(SystemExit) as exc_info:
                run_payment_confirm_command(
                    session_id="cs_test_abc",
                    timeout=30,
                    stripe_key=_STRIPE_KEY,
                    shop_dir=tmp_path,
                )

        assert exc_info.value.code == 0
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "confirmed"
        assert out["card_last4"] == "4242"
        assert out["card_brand"] == "visa"
        assert out["expiry"] == "12/2026"
        assert out["default"] is True

    def test_confirm_saves_stripe_credentials_to_yaml(self, tmp_path, capsys):
        self._write_pending(tmp_path)

        with respx.mock:
            respx.get(f"{_STRIPE_API}/checkout/sessions/cs_test_abc").mock(
                return_value=httpx.Response(200, json=_SESSION_COMPLETE)
            )
            respx.get(f"{_STRIPE_API}/setup_intents/seti_test_xyz").mock(
                return_value=httpx.Response(200, json=_SETUP_INTENT_RESPONSE)
            )
            respx.get(f"{_STRIPE_API}/payment_methods/pm_test_visa").mock(
                return_value=httpx.Response(200, json=_PAYMENT_METHOD_RESPONSE)
            )

            with pytest.raises(SystemExit):
                run_payment_confirm_command(
                    session_id="cs_test_abc",
                    timeout=30,
                    stripe_key=_STRIPE_KEY,
                    shop_dir=tmp_path,
                )
            capsys.readouterr()

        saved = yaml.safe_load((tmp_path / "payment.yaml").read_text())
        assert len(saved["methods"]) == 1
        assert len(saved["pending"]) == 0  # removed from pending

        method = saved["methods"][0]
        assert method["type"] == "stripe"
        assert method["payment_method_id"] == "pm_test_visa"
        assert method["customer_id"] == "cus_test123"
        assert method["card_last4"] == "4242"
        assert "number" not in method  # no raw card stored

    def test_confirm_yaml_chmod_600(self, tmp_path, capsys):
        self._write_pending(tmp_path)

        with respx.mock:
            respx.get(f"{_STRIPE_API}/checkout/sessions/cs_test_abc").mock(
                return_value=httpx.Response(200, json=_SESSION_COMPLETE)
            )
            respx.get(f"{_STRIPE_API}/setup_intents/seti_test_xyz").mock(
                return_value=httpx.Response(200, json=_SETUP_INTENT_RESPONSE)
            )
            respx.get(f"{_STRIPE_API}/payment_methods/pm_test_visa").mock(
                return_value=httpx.Response(200, json=_PAYMENT_METHOD_RESPONSE)
            )

            with pytest.raises(SystemExit):
                run_payment_confirm_command(
                    session_id="cs_test_abc",
                    timeout=30,
                    stripe_key=_STRIPE_KEY,
                    shop_dir=tmp_path,
                )
            capsys.readouterr()

        assert oct((tmp_path / "payment.yaml").stat().st_mode)[-3:] == "600"

    def test_confirm_missing_stripe_key_exits_1(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as exc_info:
            run_payment_confirm_command(session_id="cs_test_abc", stripe_key="", shop_dir=tmp_path)
        assert exc_info.value.code == 1
        data = json.loads(capsys.readouterr().out)
        assert data["error_code"] == "missing_stripe_key"

    def test_confirm_expired_session_exits_1(self, tmp_path, capsys):
        self._write_pending(tmp_path)

        expired = dict(_SESSION_RESPONSE, status="expired")
        with respx.mock:
            respx.get(f"{_STRIPE_API}/checkout/sessions/cs_test_abc").mock(
                return_value=httpx.Response(200, json=expired)
            )

            with pytest.raises(SystemExit) as exc_info:
                run_payment_confirm_command(
                    session_id="cs_test_abc",
                    timeout=30,
                    stripe_key=_STRIPE_KEY,
                    shop_dir=tmp_path,
                )

        assert exc_info.value.code == 1
        data = json.loads(capsys.readouterr().out)
        assert data["error_code"] == "session_expired"

    def test_confirm_timeout_exits_6(self, tmp_path, capsys, monkeypatch):
        self._write_pending(tmp_path)

        # Always return "open" so it never completes
        open_session = dict(_SESSION_RESPONSE, status="open")
        monkeypatch.setattr("shop.commands.payment._POLL_INTERVAL", 0)

        with respx.mock:
            respx.get(f"{_STRIPE_API}/checkout/sessions/cs_test_abc").mock(
                return_value=httpx.Response(200, json=open_session)
            )

            with pytest.raises(SystemExit) as exc_info:
                run_payment_confirm_command(
                    session_id="cs_test_abc",
                    timeout=0,  # immediate timeout
                    stripe_key=_STRIPE_KEY,
                    shop_dir=tmp_path,
                )

        assert exc_info.value.code == 6
        data = json.loads(capsys.readouterr().out)
        assert data["error_code"] == "timeout"

    def test_first_confirmed_becomes_default(self, tmp_path, capsys):
        self._write_pending(tmp_path)

        with respx.mock:
            respx.get(f"{_STRIPE_API}/checkout/sessions/cs_test_abc").mock(
                return_value=httpx.Response(200, json=_SESSION_COMPLETE)
            )
            respx.get(f"{_STRIPE_API}/setup_intents/seti_test_xyz").mock(
                return_value=httpx.Response(200, json=_SETUP_INTENT_RESPONSE)
            )
            respx.get(f"{_STRIPE_API}/payment_methods/pm_test_visa").mock(
                return_value=httpx.Response(200, json=_PAYMENT_METHOD_RESPONSE)
            )

            with pytest.raises(SystemExit):
                run_payment_confirm_command(
                    session_id="cs_test_abc",
                    timeout=30,
                    stripe_key=_STRIPE_KEY,
                    shop_dir=tmp_path,
                )

        out = json.loads(capsys.readouterr().out)
        assert out["default"] is True


class TestPaymentList:
    def test_empty_list(self, tmp_path, capsys):
        with pytest.raises(SystemExit):
            run_payment_list_command(shop_dir=tmp_path)
        data = json.loads(capsys.readouterr().out)
        assert data["methods"] == []
        assert data["count"] == 0
        assert data["pending_setups"] == 0

    def test_list_shows_stripe_method(self, tmp_path, capsys):
        p = tmp_path / "payment.yaml"
        p.write_text(
            yaml.dump(
                {
                    "default": "pm_test_visa",
                    "methods": [
                        {
                            "id": "pm_test_visa",
                            "label": "My Visa",
                            "type": "stripe",
                            "customer_id": "cus_test123",
                            "payment_method_id": "pm_test_visa",
                            "card_last4": "4242",
                            "card_brand": "visa",
                            "expiry": "12/2026",
                        }
                    ],
                    "pending": [],
                }
            )
        )
        p.chmod(0o600)

        with pytest.raises(SystemExit):
            run_payment_list_command(shop_dir=tmp_path)
        data = json.loads(capsys.readouterr().out)

        assert data["count"] == 1
        m = data["methods"][0]
        assert m["card_last4"] == "4242"
        assert m["card_brand"] == "visa"
        assert m["expiry"] == "12/2026"
        assert m["default"] is True
        assert "payment_method_id" not in m  # not exposed in list
        assert "customer_id" not in m

    def test_list_counts_pending_setups(self, tmp_path, capsys):
        p = tmp_path / "payment.yaml"
        p.write_text(
            yaml.dump(
                {
                    "default": None,
                    "methods": [],
                    "pending": [
                        {
                            "label": "Card 1",
                            "session_id": "cs_1",
                            "customer_id": "cus_1",
                            "email": "",
                        },
                        {
                            "label": "Card 2",
                            "session_id": "cs_2",
                            "customer_id": "cus_2",
                            "email": "",
                        },
                    ],
                }
            )
        )
        p.chmod(0o600)

        with pytest.raises(SystemExit):
            run_payment_list_command(shop_dir=tmp_path)
        data = json.loads(capsys.readouterr().out)
        assert data["count"] == 0
        assert data["pending_setups"] == 2


_STRIPE_METHOD = {
    "id": "pm_test_visa",
    "label": "My Visa",
    "type": "stripe",
    "customer_id": "cus_test123",
    "payment_method_id": "pm_test_visa",
    "card_last4": "4242",
    "card_brand": "visa",
    "expiry": "12/2026",
}
_STRIPE_METHOD_2 = {
    "id": "pm_test_mc",
    "label": "My MC",
    "type": "stripe",
    "customer_id": "cus_test456",
    "payment_method_id": "pm_test_mc",
    "card_last4": "5555",
    "card_brand": "mastercard",
    "expiry": "6/2027",
}


class TestPaymentRemove:
    def _write_methods(self, tmp_path, methods, default=None):
        data = {
            "default": default or (methods[0]["id"] if methods else None),
            "methods": methods,
            "pending": [],
        }
        p = tmp_path / "payment.yaml"
        p.write_text(yaml.dump(data))
        p.chmod(0o600)

    def test_remove_existing_method(self, tmp_path, capsys):
        self._write_methods(tmp_path, [_STRIPE_METHOD])

        with pytest.raises(SystemExit) as exc_info:
            run_payment_remove_command(method_id="pm_test_visa", shop_dir=tmp_path)
        assert exc_info.value.code == 0

        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "removed"
        assert out["remaining"] == 0

        saved = yaml.safe_load((tmp_path / "payment.yaml").read_text())
        assert saved["methods"] == []
        assert saved["default"] is None

    def test_remove_unknown_id_exits_1(self, tmp_path, capsys):
        self._write_methods(tmp_path, [_STRIPE_METHOD])

        with pytest.raises(SystemExit) as exc_info:
            run_payment_remove_command(method_id="pm_does_not_exist", shop_dir=tmp_path)
        assert exc_info.value.code == 1
        out = json.loads(capsys.readouterr().out)
        assert out["error_code"] == "not_found"

    def test_remove_default_promotes_next(self, tmp_path, capsys):
        self._write_methods(tmp_path, [_STRIPE_METHOD, _STRIPE_METHOD_2], default="pm_test_visa")

        with pytest.raises(SystemExit):
            run_payment_remove_command(method_id="pm_test_visa", shop_dir=tmp_path)
        capsys.readouterr()

        saved = yaml.safe_load((tmp_path / "payment.yaml").read_text())
        assert len(saved["methods"]) == 1
        assert saved["default"] == "pm_test_mc"

    def test_remove_non_default_keeps_default(self, tmp_path, capsys):
        self._write_methods(tmp_path, [_STRIPE_METHOD, _STRIPE_METHOD_2], default="pm_test_visa")

        with pytest.raises(SystemExit):
            run_payment_remove_command(method_id="pm_test_mc", shop_dir=tmp_path)
        capsys.readouterr()

        saved = yaml.safe_load((tmp_path / "payment.yaml").read_text())
        assert saved["default"] == "pm_test_visa"  # unchanged
