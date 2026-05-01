# shop

> **Status: alpha (v0.1.0).** Core search, mandate, cart, and order flows work. Expect breaking changes until v1.0. Feedback welcome — open an issue.

**Agent-native commerce CLI.** All commands return JSON. Humans configure once; agents invoke autonomously.

```bash
pip install shop-cli
shop search products "coffee filters" --max-price 20
```

---

## What it is

`shop` is a CLI built for AI agents as the primary user. It handles product discovery, mandate-enforced purchasing, and order tracking — with JSON output on every command and semantic exit codes agents can branch on.

Agents never see card credentials. Spending policy lives in signed mandate files, not in agent prompts.

---

## Install

```bash
pip install shop-cli
```

Requires Python 3.9+.

---

## Quick start

### 1. Add a payment method

Choose the payment network that matches your merchants:

**Stripe** (works with UCP and ACP merchants):
```bash
shop payment add --label "My Visa" --stripe-key sk_live_...
# → opens browser URL for PCI-compliant card entry
shop payment confirm --session-id cs_xxx --stripe-key sk_live_...
```

**Shop Pay** (works with Shopify UCP merchants):
```bash
shop payment add-shop-pay \
  --token SHOP_PAY_TOKEN \
  --email buyer@example.com \
  --first-name Jane --last-name Smith \
  --address1 "123 Main St" --city "Portland" --province OR --zip 97201
```

**PayPal Fastlane** (works with PayPal Fastlane merchants):
```bash
shop payment add-paypal-fastlane \
  --token FASTLANE_TOKEN \
  --email buyer@example.com
```

### 2. Connect a merchant

**Shopify Global Catalog** — search across ~1M+ Shopify stores:
```bash
shop merchant connect-shopify \
  --client-id YOUR_CLIENT_ID \
  --client-secret YOUR_CLIENT_SECRET
```

**Shopify UCP checkout** — agent checkout via Shop Pay at a specific Shopify store:
```bash
shop merchant add-shopify-checkout \
  --store-domain my-store.myshopify.com
# uses credentials from connect-shopify above
```

**UCP merchant** — any merchant publishing `/.well-known/ucp`:
```bash
shop merchant add https://store.example.com
```

**ACP merchant** — Stripe Agentic Commerce Protocol endpoint:
```bash
shop merchant add-acp https://store.example.com [--acp-key KEY]
```

**PayPal Fastlane merchant**:
```bash
shop merchant add-paypal \
  --name "Acme Store" \
  --client-id PP_CLIENT_ID \
  --client-secret PP_CLIENT_SECRET
```

### 3. Create a mandate

Mandates define what an agent is allowed to buy. They're Ed25519-signed YAML files stored in `~/.shop/mandates/`.

```bash
shop mandate create \
  --budget-total 500 \
  --per-order-max 50 \
  --period monthly \
  --category-allow "office supplies,coffee"
```

### 4. Search and buy

```bash
# Search
shop search products "coffee filters" --max-price 20 --in-stock-only

# Add to cart
shop cart add --sku shopify:abc123 --quantity 2

# Place order
shop order create --from-cart --idempotency-key $(uuidgen) --yes
```

---

## Payment methods

`shop` supports five payment credential types. Each is stored as an opaque token in `~/.shop/payment.yaml` — agents never see card numbers.

| Type | Command | Works with |
|------|---------|-----------|
| `stripe` | `shop payment add` → `shop payment confirm` | `ucp`, `acp` adapters |
| `shop_pay` | `shop payment add-shop-pay` | `shopify_ucp` adapter |
| `paypal_fastlane` | `shop payment add-paypal-fastlane` | `paypal_fastlane` adapter |
| `credit_card` | `shop payment add-card` | `shopify_storefront` (dev/test only) |

**Stripe flow:** `shop payment add` creates a Stripe Checkout Session and returns a browser URL. The user enters card details in Stripe's hosted page (PCI scope stays with Stripe). `shop payment confirm` polls until complete, then stores only opaque Stripe IDs.

```bash
shop payment list   # view stored methods (last4/brand/expiry only)
shop payment remove --id pm_xxx
```

---

## Merchant adapters

| Adapter | Protocol | Payment | Use for |
|---------|----------|---------|---------|
| `ucp` | UCP v1 REST | Stripe | UCP-compliant merchants |
| `acp` | ACP REST | Stripe | ACP-spec merchants (Stripe/OpenAI standard) |
| `shopify_catalog` | Shopify Catalog API | (search only) | Shopify product discovery |
| `shopify_storefront` | Shopify Storefront API | credit card | Per-store Shopify checkout |
| `shopify_ucp` | Shopify UCP/MCP JSON-RPC | Shop Pay | Shopify stores (agent checkout) |
| `paypal_fastlane` | PayPal Orders API v2 | PayPal Fastlane | PayPal-enabled merchants |
| `mock` | Deterministic fixtures | — | Dev / offline testing |

---

## Command reference

All commands exit 0 on success and return a JSON object on stdout. Errors also return JSON with `error_code`, `detail`, and `exit_code`.

### `shop search products`

Search across all registered merchants in parallel.

```
shop search products QUERY [--max-price FLOAT] [--min-rating FLOAT] [--in-stock-only] [--explain]
```

```json
{
  "results": [
    {
      "sku": "shopify:abc123",
      "title": "Arabica Coffee Filters 100-pack",
      "price": 12.99,
      "availability": "InStock",
      "confidence": 0.87
    }
  ],
  "total": 1,
  "meta": { "total_queried": 1, "failed_merchants": [] }
}
```

`--explain` adds a per-result `confidence_explanation` breakdown.

### `shop merchant` commands

| Command | Description |
|---------|-------------|
| `shop merchant add URL` | Discover and register UCP merchant via `/.well-known/ucp` |
| `shop merchant add-acp URL [--acp-key KEY]` | Register ACP merchant via `/.well-known/acp` |
| `shop merchant add-paypal --name N --client-id ID --client-secret S` | Register PayPal Fastlane merchant |
| `shop merchant add-shopify-store --store-domain D --storefront-token T` | Register Shopify Storefront merchant |
| `shop merchant add-shopify-checkout --store-domain D` | Register Shopify for agent UCP checkout |
| `shop merchant connect-shopify --client-id ID --client-secret S` | Connect Shopify Global Catalog |

### `shop payment` commands

| Command | Description |
|---------|-------------|
| `shop payment add --label L --stripe-key SK` | Start Stripe card setup (returns browser URL) |
| `shop payment confirm --session-id ID --stripe-key SK` | Complete Stripe setup, store credentials |
| `shop payment add-shop-pay --token T --email E` | Store Shop Pay token |
| `shop payment add-paypal-fastlane --token T --email E` | Store PayPal Fastlane token |
| `shop payment add-card --number N ...` | Store raw card (DEV/TEST ONLY) |
| `shop payment list` | List stored methods (no sensitive data) |
| `shop payment remove --id ID` | Remove a payment method |

### `shop mandate create`

```
shop mandate create \
  --budget-total FLOAT \
  --per-order-max FLOAT \
  --period monthly|weekly|one-time \
  [--category-allow "cat1,cat2"] \
  [--category-deny "cat1,cat2"] \
  [--merchant-allow "slug1,slug2"] \
  [--merchant-deny "slug1,slug2"] \
  [--expires-at ISO8601]
```

### `shop mandate list / verify / usage`

```bash
shop mandate list                        # all mandates with budget utilization
shop mandate verify --mandate-id ID      # check Ed25519 signature
shop mandate usage --mandate-id ID       # live budget + pending orders
```

### `shop cart add / view / clear`

```bash
shop cart add --sku MERCHANT:SKU [--quantity 1] [--dry-run]
shop cart view [--session-id ID]
shop cart clear --yes [--session-id ID]
```

`--dry-run` validates mandate compliance and confidence scoring without writing to the database.

### `shop order create / status`

```bash
shop order create \
  --from-cart \
  --idempotency-key UUID \
  --yes \
  [--mandate-id ID]

shop order status --order-id ID
```

Every order requires `--idempotency-key` — safe to retry on network failure.

### `shop history`

```bash
shop history [--last 20] [--merchant slug]
```

Transaction audit log from local SQLite. Never leaves the machine.

### `shop schema commands`

```bash
shop schema commands
```

Returns the full machine-readable CLI contract — all commands, flags, types, and exit codes. Agents use this for runtime capability discovery without reading docs.

---

## Exit codes

Agents branch on exit codes rather than parsing error text.

| Code | Meaning | Agent action |
|------|---------|-------------|
| `0` | Success | Proceed |
| `1` | Bad arguments | Fix the call |
| `2` | Auth error | Re-authenticate |
| `3` | Mandate violation | Stop or request approval |
| `4` | Unavailable / not supported | Try another merchant |
| `5` | Low confidence | Surface to human |
| `6` | Network / DB error | Retry (safe) |

---

## Confidence scoring

Every search result includes a `confidence` score (0.0–1.0) computed from six signals:

| Signal | Weight | Description |
|--------|--------|-------------|
| `fields_completeness` | 30% | 7 required fields; each missing costs −10% |
| `seller_rating` | 20% | ≥4.5 → full score; <3.5 → zero |
| `review_count` | 20% | ≥50 → full; 10–49 → 70%; 1–9 → 40% |
| `return_policy` | 15% | return window + condition + refund timeline |
| `certifications` | 10% | any cert present → full score |
| `price_stability` | 5% | 30-day max/min ratio ≤1.10 → stable |

Default threshold is **0.80** — results below threshold cause exit 5. Configure in `~/.shop/config.yaml`:

```yaml
confidence_threshold: 0.80
default_mandate: mandate-id-here
max_workers: 10
```

---

## Configuration

| Path | Purpose |
|------|---------|
| `~/.shop/config.yaml` | Global settings (threshold, max_workers, default mandate) |
| `~/.shop/merchants.yaml` | Registered merchants and adapter credentials |
| `~/.shop/payment.yaml` | Payment credentials (chmod 600) |
| `~/.shop/mandates/` | Ed25519-signed mandate files |
| `~/.shop/shop.db` | SQLite order history and cart state |
| `~/.shop/keys/` | Ed25519 mandate key, P-256 UCP signing key |

Override the config directory with `SHOP_HOME=/path/to/dir` — useful in read-only home environments or for testing.

---

## For agents

### Idiomatic agent workflow

```bash
# 1. Discover what's available
shop schema commands | jq '.commands[].noun' | sort -u

# 2. Create a mandate (once, at session start)
MANDATE=$(shop mandate create --budget-total 200 --per-order-max 40 --period monthly | jq -r '.mandate_id')

# 3. Search with confidence filter
shop search products "USB-C hub" --max-price 40 --in-stock-only

# 4. Validate before committing
shop cart add --sku shopify:xyz --dry-run

# 5. Place order with idempotency key
shop order create --from-cart --mandate-id $MANDATE \
  --idempotency-key $(uuidgen) --yes
```

### Handling exit codes

```python
import subprocess, json

result = subprocess.run(["shop", "search", "products", "coffee"], capture_output=True)
data = json.loads(result.stdout)

match result.returncode:
    case 0: process(data["results"])
    case 3: request_approval(data["detail"])   # mandate violation
    case 5: surface_to_human(data["results"])  # low confidence
    case 6: retry()                            # network error, safe to retry
```

---

## Python versions

Tested on Python 3.9, 3.10, 3.11, 3.12.

## License

MIT
