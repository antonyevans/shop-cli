"""Integration test fixtures.

Integration tests invoke `shop` as a subprocess (black-box), so they test
the full CLI stack: arg parsing, config loading, adapter, and output format.

Credential tiers (controlled by env vars — missing vars skip those tests):
  Tier 0: no credentials  → ACP stub, catalog search smoke test
  Tier 1: SHOP_STRIPE_SECRET_KEY  → ACP real-payment
  Tier 2: SHOP_PAYPAL_CLIENT_ID + SHOP_PAYPAL_CLIENT_SECRET  → PayPal sandbox
          SHOP_BOLT_API_KEY + SHOP_BOLT_MERCHANT_ID  → Bolt sandbox
  Tier 3: SHOP_SHOPIFY_CLIENT_ID + SHOP_SHOPIFY_CLIENT_SECRET
           + SHOP_SHOPIFY_SHOP_PAY_TOKEN  → Shopify UCP

For local runs, copy .env.integration.example → .env.integration and fill in
your sandbox credentials. The runner script sources this file automatically.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

from tests.integration._helpers import shop, skip_unless  # noqa: F401 — re-exported for convenience

# ── isolated shop home ──────────────────────────────────────────────────────


@pytest.fixture()
def shop_home(tmp_path: Path) -> Path:
    """Fresh SHOP_HOME for each test — no bleed between tests."""
    h = tmp_path / "shop"
    h.mkdir()
    (h / "payment.yaml").write_text("default: null\nmethods: []\npending: []\n")
    (h / "payment.yaml").chmod(0o600)
    return h


@pytest.fixture()
def shop_home_with_stripe(shop_home: Path) -> Path:
    """SHOP_HOME pre-loaded with a Stripe test credential from ~/.shop/payment.yaml.

    Reads the real payment.yaml (set up during the Stripe integration test on 2026-03-27).
    Copies stripe-type credentials only.
    """
    real = Path.home() / ".shop" / "payment.yaml"
    if real.exists():
        shutil.copy(real, shop_home / "payment.yaml")
        (shop_home / "payment.yaml").chmod(0o600)
    return shop_home


@pytest.fixture()
def mandate_id(shop_home: Path) -> str:
    """Create a test mandate and return its ID."""
    out = shop(
        shop_home,
        "mandate",
        "create",
        "--budget-total",
        "500",
        "--per-order-max",
        "100",
        "--period",
        "monthly",
    )
    return out["mandate_id"]


# ── ACP demo server ─────────────────────────────────────────────────────────

_ACP_SERVER_SCRIPT = Path(__file__).parent / "_acp_server.py"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def acp_server(tmp_path: Path):
    """Start the stdlib ACP demo server on a random port.

    Yields (port, acp_key) — the server runs for the duration of the test.
    Works in stub mode (no Stripe key) or real mode (SHOP_STRIPE_SECRET_KEY set).
    """
    port = _free_port()
    env = {
        **os.environ,
        "PORT": str(port),
        "BASE_URL": f"http://localhost:{port}",
    }
    stripe_key = os.environ.get("SHOP_STRIPE_SECRET_KEY", "")
    if stripe_key:
        env["STRIPE_SECRET_KEY"] = stripe_key

    proc = subprocess.Popen(
        ["python3", str(_ACP_SERVER_SCRIPT)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    # Wait for server to accept connections
    deadline = time.time() + 5.0
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:
        proc.kill()
        raise RuntimeError(f"ACP demo server did not start on port {port}")

    yield port, "shop-cli-test-acp-key"

    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=3)


@pytest.fixture()
def acp_merchant(shop_home: Path, acp_server) -> tuple[Path, str]:
    """Register the running ACP demo server as a merchant in shop_home.

    Writes merchants.yaml directly (bypasses SSRF guard — localhost only for tests).
    Adds a stub Stripe credential — the stub server doesn't validate it, but the
    ACPAdapter requires one before making the request.
    Returns (shop_home, merchant_slug).
    """
    port, acp_key = acp_server
    slug = "acp-test-local"

    (shop_home / "merchants.yaml").write_text(
        textwrap.dedent(f"""\
        merchants:
        - slug: {slug}
          name: ACP Demo Merchant
          adapter: acp
          base_url: http://localhost:{port}
          acp_endpoint: http://localhost:{port}/api/acp
          acp_key: {acp_key}
    """)
    )

    # Seed a stub Stripe credential. The stub server logs but doesn't validate these.
    # If SHOP_STRIPE_SECRET_KEY is set, copy the real credentials instead.
    real_payment = Path.home() / ".shop" / "payment.yaml"
    if real_payment.exists():
        import shutil

        shutil.copy(real_payment, shop_home / "payment.yaml")
    else:
        (shop_home / "payment.yaml").write_text(
            textwrap.dedent("""\
            default: pm_stub_test
            methods:
            - id: pm_stub_test
              type: stripe
              label: Stub Test Card
              customer_id: cus_stub_test_0000000000
              payment_method_id: pm_stub_test
              card_brand: visa
              card_last4: '4242'
              expiry: 12/2030
            pending: []
        """)
        )
    (shop_home / "payment.yaml").chmod(0o600)

    return shop_home, slug
