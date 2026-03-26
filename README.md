# shop

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

Or download a self-contained binary from [Releases](https://github.com/antonyevans/shop-cli/releases) (no Python required):

| Platform | Binary |
|----------|--------|
| Linux x86-64 | `shop-linux-amd64` |
| macOS Intel | `shop-darwin-amd64` |
| macOS Apple Silicon | `shop-darwin-arm64` |
| Windows x86-64 | `shop-windows-amd64.exe` |

---

## Quick start

### 1. Connect a merchant

**Shopify Global Catalog** — one credential searches ~1M+ Shopify stores:

```bash
shop merchant connect-shopify \
  --client-id YOUR_CLIENT_ID \
  --client-secret YOUR_CLIENT_SECRET
```

Get credentials at [partners.shopify.com](https://partners.shopify.com) → Apps → Create app → Catalog API.

**UCP merchant** — any merchant that publishes a `/.well-known/ucp` Business Profile:

```bash
shop merchant add https://store.example.com
```

### 2. Create a mandate

Mandates define what an agent is allowed to buy. They're Ed25519-signed YAML files stored in `~/.shop/mandates/`.

```bash
shop mandate create \
  --budget-total 500 \
  --per-order-max 50 \
  --period monthly \
  --category-allow "office supplies,coffee"
```

### 3. Search and buy

```bash
# Search
shop search products "coffee filters" --max-price 20 --in-stock-only

# Add to cart
shop cart add --sku shopify:abc123 --quantity 2

# Place order
shop order create --from-cart --idempotency-key $(uuidgen) --yes
```

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

`--explain` adds a per-result `confidence_explanation` breakdown (see [Confidence scoring](#confidence-scoring)).

### `shop merchant add`

Discover and register a UCP-compatible merchant.

```
shop merchant add URL
```

Fetches `URL/.well-known/ucp`, validates the Business Profile, and saves to `~/.shop/merchants.yaml`.

### `shop merchant connect-shopify`

Connect Shopify Global Catalog (one credential, all Shopify merchants).

```
shop merchant connect-shopify --client-id ID --client-secret SECRET [--ships-to US]
```

### `shop mandate create`

Create a new spending mandate.

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
| `~/.shop/merchants.yaml` | Registered merchants |
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
