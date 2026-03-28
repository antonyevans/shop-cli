#!/usr/bin/env bash
# Run integration tests. Loads .env.integration if present.
# Usage:
#   ./scripts/run-integration-tests.sh            # all tiers (uses whatever creds are set)
#   ./scripts/run-integration-tests.sh acp        # ACP tests only
#   ./scripts/run-integration-tests.sh shopify    # Shopify tests only
#   ./scripts/run-integration-tests.sh paypal     # PayPal tests only
#   ./scripts/run-integration-tests.sh bolt       # Bolt tests only

set -euo pipefail

cd "$(dirname "$0")/.."

# Load local credentials if file exists
if [[ -f .env.integration ]]; then
    echo "Loading credentials from .env.integration"
    set -a
    # shellcheck disable=SC1091
    source .env.integration
    set +a
fi

# Ensure shop is installed
if ! command -v shop &>/dev/null; then
    echo "Installing shop-cli..."
    pip install -e ".[dev]" -q
fi

FILTER="${1:-}"
shift || true  # consume the filter arg so $@ doesn't re-pass it to pytest

if [[ -n "$FILTER" ]]; then
    TEST_PATH="tests/integration/test_${FILTER}_integration.py"
    if [[ ! -f "$TEST_PATH" ]]; then
        echo "Unknown filter: $FILTER"
        echo "Available: acp, shopify, paypal, bolt"
        exit 1
    fi
else
    TEST_PATH="tests/integration"
fi

echo ""
echo "══════════════════════════════════════════════"
echo "  shop-cli integration tests"
if [[ -n "$FILTER" ]]; then
    echo "  Filter: $FILTER"
fi
echo "══════════════════════════════════════════════"
echo ""

# Print which tiers are active
echo "Active credential tiers:"
[[ -n "${SHOP_STRIPE_SECRET_KEY:-}" ]]        && echo "  ✅ Tier 1: ACP real-payment (Stripe)"     || echo "  ⏭  Tier 1: ACP real-payment (SHOP_STRIPE_SECRET_KEY not set)"
[[ -n "${SHOP_SHOPIFY_CLIENT_ID:-}" ]]         && echo "  ✅ Tier 2: Shopify Catalog search"        || echo "  ⏭  Tier 2: Shopify Catalog (SHOP_SHOPIFY_CLIENT_ID not set)"
[[ -n "${SHOP_SHOPIFY_SHOP_PAY_TOKEN:-}" ]]   && echo "  ✅ Tier 3: Shopify UCP checkout"          || echo "  ⏭  Tier 3: Shopify UCP (SHOP_SHOPIFY_SHOP_PAY_TOKEN not set)"
[[ -n "${SHOP_PAYPAL_CLIENT_ID:-}" ]]         && echo "  ✅ Tier 2: PayPal Fastlane"               || echo "  ⏭  Tier 2: PayPal (SHOP_PAYPAL_CLIENT_ID not set)"
[[ -n "${SHOP_BOLT_API_KEY:-}" ]]             && echo "  ✅ Tier 2: Bolt"                          || echo "  ⏭  Tier 2: Bolt (SHOP_BOLT_API_KEY not set)"
echo ""
echo "Tier 0 (ACP stub): always runs"
echo ""

pytest "$TEST_PATH" \
    -m integration \
    -v \
    --tb=short \
    --no-header \
    -p no:cacheprovider \
    "$@"
