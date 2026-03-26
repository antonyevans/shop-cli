"""shop schema commands — machine-readable CLI contract for agent introspection."""

from __future__ import annotations

import json
import sys

import typer

app = typer.Typer()

# Full v0 command surface. Kept as a literal constant so agents can trust it is static.
_COMMANDS = [
    {
        "noun": "search",
        "verb": "products",
        "description": "Search for products across registered merchants",
        "flags": [
            {"name": "query", "type": "string", "required": True, "description": "Search terms"},
            {"name": "max-price", "type": "float", "required": False, "description": "Maximum price in USD"},
            {"name": "min-rating", "type": "float", "required": False, "description": "Minimum seller rating (0-5)"},
            {"name": "in-stock-only", "type": "bool", "required": False, "description": "Filter to in-stock only"},
            {"name": "explain", "type": "bool", "required": False, "description": "Include confidence breakdown"},
        ],
        "exit_codes": [0, 4, 5, 6],
        "mutates": False,
    },
    {
        "noun": "product",
        "verb": "info",
        "description": "Get full product detail for a SKU",
        "flags": [
            {"name": "sku", "type": "string", "required": True, "description": "Product SKU"},
        ],
        "exit_codes": [0, 4, 6],
        "mutates": False,
    },
    {
        "noun": "cart",
        "verb": "add",
        "description": "Add a product to cart. Use --dry-run to validate mandate compliance without committing.",
        "flags": [
            {"name": "sku", "type": "string", "required": True, "description": "Product SKU"},
            {"name": "quantity", "type": "int", "required": False, "default": 1, "description": "Quantity"},
            {"name": "session-id", "type": "string", "required": False, "description": "Cart session ID for multi-step workflows"},
            {"name": "dry-run", "type": "bool", "required": False, "description": "Validate without committing"},
            {"name": "idempotency-key", "type": "string", "required": False, "description": "Idempotency key"},
        ],
        "exit_codes": [0, 3, 4, 5],
        "mutates": True,
    },
    {
        "noun": "cart",
        "verb": "view",
        "description": "View current cart contents",
        "flags": [
            {"name": "session-id", "type": "string", "required": False, "description": "Cart session ID"},
        ],
        "exit_codes": [0],
        "mutates": False,
    },
    {
        "noun": "cart",
        "verb": "clear",
        "description": "Clear all items from cart",
        "flags": [
            {"name": "session-id", "type": "string", "required": False, "description": "Cart session ID"},
            {"name": "yes", "type": "bool", "required": True, "description": "Confirm clear"},
        ],
        "exit_codes": [0],
        "mutates": True,
    },
    {
        "noun": "order",
        "verb": "create",
        "description": "Execute purchase from cart or direct SKU. Requires a mandate.",
        "flags": [
            {"name": "from-cart", "type": "bool", "required": False, "description": "Use items from current cart session"},
            {"name": "session-id", "type": "string", "required": False, "description": "Cart session ID (required with --from-cart)"},
            {"name": "sku", "type": "string", "required": False, "description": "Direct SKU purchase (alternative to --from-cart)"},
            {"name": "quantity", "type": "int", "required": False, "default": 1, "description": "Quantity for direct SKU purchase"},
            {"name": "mandate-id", "type": "string", "required": False, "description": "Mandate ID (defaults to config default_mandate)"},
            {"name": "idempotency-key", "type": "string", "required": True, "description": "Required for safe retry"},
            {"name": "yes", "type": "bool", "required": True, "description": "Confirm purchase"},
        ],
        "exit_codes": [0, 2, 3, 4, 5, 6],
        "mutates": True,
    },
    {
        "noun": "order",
        "verb": "status",
        "description": "Get status of a specific order",
        "flags": [
            {"name": "order-id", "type": "string", "required": True, "description": "Order ID"},
        ],
        "exit_codes": [0, 4, 6],
        "mutates": False,
    },
    {
        "noun": "mandate",
        "verb": "create",
        "description": "Create a new Ed25519-signed spending mandate",
        "flags": [
            {"name": "budget-total", "type": "float", "required": True, "description": "Total budget in USD"},
            {"name": "per-order-max", "type": "float", "required": True, "description": "Per-order maximum in USD"},
            {"name": "period", "type": "string", "required": True, "description": "monthly | weekly | one-time"},
            {"name": "category-allow", "type": "string", "required": False, "description": "Comma-separated allowed categories"},
            {"name": "category-deny", "type": "string", "required": False, "description": "Comma-separated denied categories"},
            {"name": "merchant-allow", "type": "string", "required": False, "description": "Comma-separated allowed merchant slugs"},
            {"name": "merchant-deny", "type": "string", "required": False, "description": "Comma-separated denied merchant slugs"},
            {"name": "expires-at", "type": "string", "required": False, "description": "ISO8601 expiry timestamp"},
        ],
        "exit_codes": [0, 1],
        "mutates": True,
    },
    {
        "noun": "mandate",
        "verb": "list",
        "description": "List all mandates with budget utilization",
        "flags": [],
        "exit_codes": [0],
        "mutates": False,
    },
    {
        "noun": "mandate",
        "verb": "verify",
        "description": "Verify Ed25519 signature and tamper detection for a mandate",
        "flags": [
            {"name": "mandate-id", "type": "string", "required": True, "description": "Mandate ID to verify"},
        ],
        "exit_codes": [0, 1],
        "mutates": False,
    },
    {
        "noun": "mandate",
        "verb": "usage",
        "description": "Get budget utilization and pending orders for a mandate",
        "flags": [
            {"name": "mandate-id", "type": "string", "required": False, "description": "Defaults to config default_mandate"},
        ],
        "exit_codes": [0, 1],
        "mutates": False,
    },
    {
        "noun": "merchant",
        "verb": "add",
        "description": "Register a UCP-compatible merchant by HTTPS URL (discovers via /.well-known/ucp)",
        "flags": [
            {"name": "url", "type": "string", "required": True, "description": "Merchant HTTPS URL"},
        ],
        "exit_codes": [0, 1, 4],
        "mutates": True,
    },
    {
        "noun": "merchant",
        "verb": "connect-shopify",
        "description": "Connect Shopify Global Catalog — one credential searches all Shopify merchants",
        "flags": [
            {"name": "client-id", "type": "string", "required": True, "description": "Shopify Dev Dashboard app client ID"},
            {"name": "client-secret", "type": "string", "required": True, "description": "Shopify Dev Dashboard app client secret"},
            {"name": "ships-to", "type": "string", "required": False, "default": "US", "description": "ISO 3166-1 alpha-2 country code"},
        ],
        "exit_codes": [0, 2, 6],
        "mutates": True,
    },
    {
        "noun": "payment",
        "verb": "add",
        "description": "Start Stripe card setup — returns a browser URL. Card details entered in Stripe, never by the agent.",
        "flags": [
            {"name": "label", "type": "string", "required": True, "description": "Name for this payment method"},
            {"name": "email", "type": "string", "required": False, "description": "Customer email for Stripe records"},
            {"name": "stripe-key", "type": "string", "required": False, "description": "Stripe secret key (or STRIPE_SECRET_KEY env var)"},
        ],
        "exit_codes": [0, 1, 2, 6],
        "mutates": True,
    },
    {
        "noun": "payment",
        "verb": "confirm",
        "description": "Poll Stripe until card setup completes, then store credentials locally (chmod 600).",
        "flags": [
            {"name": "session-id", "type": "string", "required": True, "description": "Stripe checkout session ID from `payment add`"},
            {"name": "timeout", "type": "int", "required": False, "default": 300, "description": "Max seconds to wait"},
            {"name": "stripe-key", "type": "string", "required": False, "description": "Stripe secret key (or STRIPE_SECRET_KEY env var)"},
        ],
        "exit_codes": [0, 1, 2, 6],
        "mutates": True,
    },
    {
        "noun": "payment",
        "verb": "list",
        "description": "List stored payment methods (no sensitive data — only last4, brand, expiry).",
        "flags": [],
        "exit_codes": [0],
        "mutates": False,
    },
    {
        "noun": "history",
        "verb": None,
        "description": "View transaction history from local SQLite audit log. Note: no verb — this command is `shop history [flags]`.",
        "flags": [
            {"name": "last", "type": "int", "required": False, "default": 20, "description": "Number of most recent records"},
            {"name": "merchant", "type": "string", "required": False, "description": "Filter by merchant slug"},
        ],
        "exit_codes": [0],
        "mutates": False,
    },
    {
        "noun": "schema",
        "verb": "commands",
        "description": "List all available commands with flags, types, and exit codes. This output is this schema.",
        "flags": [],
        "exit_codes": [0],
        "mutates": False,
    },
]


@app.command("commands")
def schema_commands() -> None:
    """Return machine-readable schema for all shop commands."""
    print(json.dumps({"commands": _COMMANDS}, ensure_ascii=False))
    sys.exit(0)
