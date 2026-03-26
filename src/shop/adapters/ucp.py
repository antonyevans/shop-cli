"""UCPAdapter — HTTP adapter for UCP-compatible merchants.

UCP v1 (2026-01-23) is a checkout + order lifecycle protocol only.
There are NO search or product detail endpoints in the spec.
Product discovery is handled by ShopifyCatalogAdapter or other means.

Endpoint reference: https://ucp.dev/2026-01-23/services/shopping/openapi.json

Payment integration:
  If ~/.shop/payment.yaml contains a Stripe credential (type: stripe),
  its customer_id and payment_method_id are included in the checkout-session
  body under a `payment` key. UCP merchants that support Stripe can use these
  to charge the buyer directly. Merchants that don't support Stripe ignore the
  field — it is always optional.
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from pathlib import Path

import httpx
import yaml
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from shop.adapters.base import (
    AdapterError,
    CheckoutNotSupportedError,
    MerchantAdapter,
    ProductNotFoundError,
)
from shop.models.commerce import CommerceTXTProduct, SearchFilters

_TIMEOUT = 5.0


def _get_shop_dir() -> Path:
    return Path(os.environ["SHOP_HOME"]) if "SHOP_HOME" in os.environ else Path.home() / ".shop"


def _load_stripe_payment(shop_dir: Path) -> dict | None:
    """Return Stripe payment credentials from payment.yaml, or None if unavailable.

    Only returns credentials if a confirmed Stripe method (type: stripe) exists.
    Silently returns None if payment.yaml is missing, empty, or has no Stripe method.
    """
    payment_path = shop_dir / "payment.yaml"
    if not payment_path.exists():
        return None
    try:
        with payment_path.open() as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return None

    methods = data.get("methods", [])
    if not methods:
        return None

    default_id = data.get("default")
    method = (
        next((m for m in methods if m["id"] == default_id), None)
        or methods[0]
    )
    if method.get("type") != "stripe":
        return None

    customer_id = method.get("customer_id")
    pm_id = method.get("payment_method_id")
    if not customer_id or not pm_id:
        return None

    return {"stripe_customer_id": customer_id, "stripe_payment_method_id": pm_id}


def _load_or_create_signing_key() -> ec.EllipticCurvePrivateKey:
    """Load P-256 signing key from ~/.shop/keys/ucp_signing.pem, creating if absent."""
    key_path = _get_shop_dir() / "keys" / "ucp_signing.pem"
    key_path.parent.mkdir(parents=True, exist_ok=True)

    if key_path.exists():
        with key_path.open("rb") as f:
            return serialization.load_pem_private_key(f.read(), password=None)

    key = ec.generate_private_key(ec.SECP256R1())
    with key_path.open("wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    key_path.chmod(0o600)
    return key


def _make_request_signature(body: bytes) -> str:
    """Create ES256 JWS detached-payload signature over request body.

    Format: base64url(header)..base64url(signature)
    Algorithm: ECDSA P-256 / SHA-256 (ES256)
    """
    key = _load_or_create_signing_key()

    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "ES256", "typ": "JWS"}, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(body).rstrip(b"=").decode()

    signing_input = f"{header}.{payload}".encode()
    sig_der = key.sign(signing_input, ec.ECDSA(hashes.SHA256()))

    # DER → raw (r‖s, 32 bytes each) per JWS ES256
    r, s = decode_dss_signature(sig_der)
    raw_sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    sig_b64 = base64.urlsafe_b64encode(raw_sig).rstrip(b"=").decode()

    return f"{header}..{sig_b64}"  # detached payload compact serialization


def _ucp_headers(body: bytes, idempotency_key: str) -> dict:
    """Build required UCP request headers."""
    return {
        "Content-Type": "application/json",
        "Request-Id": str(uuid.uuid4()),
        "Idempotency-Key": idempotency_key,
        "Request-Signature": _make_request_signature(body),
    }


class UCPAdapter(MerchantAdapter):
    """Adapter for UCP-compatible merchants (checkout + order lifecycle only).

    UCP v1 has NO search or catalog endpoints. search() and get_product()
    raise AdapterError — use ShopifyCatalogAdapter for product discovery.

    The checkout flow follows the UCP shopping service spec:
      POST /checkout-sessions       → create session
      POST /checkout-sessions/{id}/complete → place order
    """

    def __init__(self, slug: str, config: dict) -> None:
        super().__init__(slug, config)
        self.ucp_endpoint = config.get("ucp_endpoint", "").rstrip("/")

    async def search(self, query: str, filters: SearchFilters) -> list[CommerceTXTProduct]:
        raise AdapterError(
            self.slug,
            "UCP does not support product search. Use ShopifyCatalogAdapter for discovery.",
        )

    async def get_product(self, sku: str) -> CommerceTXTProduct:
        raise ProductNotFoundError(
            self.slug,
            "UCP does not expose product detail endpoints. Use ShopifyCatalogAdapter.",
        )

    async def get_capabilities(self) -> dict:
        """Fetch capabilities from /.well-known/ucp profile."""
        base = "/".join(self.ucp_endpoint.split("/")[:3])  # scheme + host
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                r = await client.get(f"{base}/.well-known/ucp")
                if r.status_code == 200:
                    profile = r.json()
                    services = profile.get("ucp", {}).get("services", [])
                    caps = []
                    for svc in services:
                        caps.extend(svc.get("capabilities", []))
                    return {"capabilities": caps, "ucp_version": profile.get("ucp", {}).get("version")}
        except Exception:
            pass
        return {}

    async def create_order(
        self, sku: str, quantity: int, mandate_id: str, idempotency_key: str,
        checkout_url: str | None = None,
    ) -> dict:
        """Execute a UCP checkout: create session → complete session.

        Phase 1: POST /checkout-sessions (create cart session)
        Phase 2: POST /checkout-sessions/{id}/complete (place order)
        """
        if not self.ucp_endpoint:
            raise AdapterError(self.slug, "No ucp_endpoint configured")

        raw_sku = sku.removeprefix(f"{self.slug}:")

        # Phase 1: create checkout session
        # Include Stripe payment credentials if available — UCP merchants that
        # support Stripe use these to charge the buyer. Others ignore the field.
        session_payload: dict = {
            "items": [{"sku": raw_sku, "quantity": quantity}],
            "mandate_id": mandate_id,
        }
        stripe_creds = _load_stripe_payment(_get_shop_dir())
        if stripe_creds:
            session_payload["payment"] = stripe_creds

        session_body = json.dumps(session_payload, separators=(",", ":")).encode()

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                r = await client.post(
                    f"{self.ucp_endpoint}/checkout-sessions",
                    content=session_body,
                    headers=_ucp_headers(session_body, idempotency_key),
                )
                if r.status_code == 409:
                    # Idempotency conflict — return existing session
                    return r.json()
                r.raise_for_status()
                session = r.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (501, 503):
                raise CheckoutNotSupportedError(self.slug, "Merchant does not support checkout")
            raise AdapterError(self.slug, f"HTTP {e.response.status_code} on session create")
        except httpx.TimeoutException:
            raise TimeoutError()
        except Exception as e:
            raise AdapterError(self.slug, str(e))

        session_id = session.get("id") or session.get("session_id")
        if not session_id:
            raise AdapterError(self.slug, "No session_id in checkout-sessions response")

        # Phase 2: complete (place order)
        complete_key = f"{idempotency_key}-complete"
        complete_body = json.dumps({}, separators=(",", ":")).encode()

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                r = await client.post(
                    f"{self.ucp_endpoint}/checkout-sessions/{session_id}/complete",
                    content=complete_body,
                    headers=_ucp_headers(complete_body, complete_key),
                )
                r.raise_for_status()
                return r.json()
        except httpx.HTTPStatusError as e:
            raise AdapterError(self.slug, f"HTTP {e.response.status_code} on session complete")
        except httpx.TimeoutException:
            raise TimeoutError()
        except Exception as e:
            raise AdapterError(self.slug, str(e))
