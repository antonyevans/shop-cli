"""Minimal ACP demo server for shop-cli integration testing.

Two modes:
- With STRIPE_SECRET_KEY: creates real PaymentIntents (real payment test)
- Without it: returns stub order (adapter wiring test)
"""
import os, uuid, json
from http.server import BaseHTTPRequestHandler, HTTPServer

STRIPE_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
ACP_KEY = "shop-cli-test-acp-key"
PORT = int(os.environ.get("PORT", "8765"))
BASE_URL = os.environ.get("BASE_URL", f"http://localhost:{PORT}")


class ACPHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[ACP] {fmt % args}")

    def _respond(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/.well-known/acp":
            self._respond(200, {
                "version": "1.0",
                "name": "ACP Demo Merchant",
                "acp": {
                    "endpoint": f"{BASE_URL}/api/acp",
                    "payment_handlers": ["stripe"],
                    "currency": "USD",
                }
            })
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/api/acp/checkout":
            self._respond(404, {"error": "not found"})
            return

        auth = self.headers.get("Authorization", "")
        if auth != f"Bearer {ACP_KEY}":
            self._respond(401, {"error": "unauthorized"})
            return

        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        idem_key = self.headers.get("Idempotency-Key", str(uuid.uuid4()))

        payment = body.get("payment", {})
        customer_id = payment.get("customer_id")
        pm_id = payment.get("payment_method_id")
        items = body.get("items", [])

        print(f"  customer_id: {customer_id}")
        print(f"  pm_id: {pm_id}")
        print(f"  items: {items}")
        print(f"  mandate_id: {body.get('mandate_id')}")
        print(f"  idempotency_key: {idem_key}")

        if STRIPE_KEY:
            try:
                import stripe
                stripe.api_key = STRIPE_KEY
                intent = stripe.PaymentIntent.create(
                    amount=999,
                    currency="usd",
                    customer=customer_id,
                    payment_method=pm_id,
                    confirm=True,
                    off_session=True,
                    idempotency_key=idem_key,
                )
                order_id = f"acp-{intent.id}"
                print(f"  Stripe intent: {intent.id} status={intent.status}")
            except Exception as e:
                print(f"  Stripe error: {e}")
                self._respond(422, {"error": "payment_failed", "message": str(e)})
                return
        else:
            # Stub mode — return fake order without charging
            order_id = f"acp-stub-{uuid.uuid4().hex[:8]}"
            print("  STUB MODE — no real payment")

        self._respond(200, {
            "order_id": order_id,
            "status": "confirmed",
            "total_cents": 999,
            "currency": "USD",
            "confirmation_code": f"ACP-{order_id[-8:].upper()}",
        })


if __name__ == "__main__":
    mode = "REAL (Stripe)" if STRIPE_KEY else "STUB (no Stripe key)"
    print(f"ACP Demo Server — {mode}")
    print(f"Listening on port {PORT}")
    print(f"BASE_URL: {BASE_URL}")
    HTTPServer(("0.0.0.0", PORT), ACPHandler).serve_forever()
