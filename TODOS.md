# TODOS

## v0 Phase Milestone

### PHASE v0: Build search end-to-end before touching checkout
**What:** Complete `shop search products` (parallel UCP queries, confidence scoring, normalized JSON output) and prove it works against at least one real merchant before starting mandate/cart/order implementation.

**Why:** v0 is making two independent bets simultaneously — (1) UCP-based discovery works better than Playwright scraping, and (2) local mandate enforcement is the right trust model. These bets are independent. Shipping search first validates bet 1 before bet 2 adds implementation complexity. Both the CEO review and the Codex outside voice flagged that building both simultaneously is the highest-risk path to never shipping.

**How to apply:** Implement in this order:
1. `shop search products` — UCP adapter(s), confidence scorer, JSON output, `--explain` flag
2. `shop merchant add` — merchant registry, UCP discovery
3. `shop schema commands` — runtime self-description
4. Only then: mandate, cart, order, history

**Milestone gate:** Before writing any mandate/cart/order code, `shop search products "coffee filters"` must return real results from at least one UCP-compatible merchant (or MockAdapter deterministic fixtures if Shopify auth requires per-merchant credentials).

**Effort:** No extra effort — this is sequencing, not additional scope.
**Blocked by:** Nothing — this is the first thing to implement.

---

## Pre-implementation

### VALIDATE: Shopify global catalog auth requirements
**What:** Before writing any code, confirm whether `search_global_products` in Shopify's
Catalog MCP server is publicly accessible or requires merchant-specific API credentials.

**Why:** The entire v0 discovery proof (cross-Shopify-merchant search) depends on this
call working. If it requires per-merchant auth, the discovery wedge needs a major
architectural revision.

**How to validate:** Check `shopify.dev/docs/agents/catalog` — specifically auth
requirements for the Catalog MCP server. Attempt a test call in sandbox mode. Should
take 30 minutes.

**If auth is required:** The fallback is Shopify Storefront API search (per-store, requires
partner access per store) or pivoting to a different discovery mechanism. Revisit the
architecture before starting implementation.

**Blocked by:** Nothing — do this first.
**Blocks:** All implementation work.

---

### VALIDATE: UCP spec accessibility and stability
**What:** Before writing any code, confirm the Universal Commerce Protocol (UCP) spec is
publicly accessible, has a stable API, and is implementable without special Google
partnership access.

**Why:** 8 of the 9 shop commands depend on UCP. If the spec is behind a partnership
agreement or still in flux, the architecture needs revision before implementation starts.

**How to validate:** Read the spec at `developers.googleblog.com/under-the-hood-universal-commerce-protocol-ucp/`
and verify: (1) endpoint URL patterns are documented, (2) auth model is clear, (3) the
catalog/search capability is in the spec (not just checkout). Should take 30 minutes.

**If UCP is not implementable:** Fall back to Shopify Storefront API + Stripe only for v0.
The CLI shape remains identical; only the adapters change.

**Blocked by:** Nothing — do this alongside Shopify auth validation.
**Blocks:** All UCP adapter implementation.

---

## Pre-ship (v0)

### CI/CD pipeline
**What:** GitHub Actions workflow: lint (ruff), test (pytest --cov), publish to PyPI on tag, PyInstaller binary builds for linux/darwin/windows × amd64/arm64. Also: `pip install shop` package name reservation on PyPI.

**Why:** Without this, v0 can't be installed by anyone. `pip install shop` is the first command in every agent developer tutorial — it must work before anything else.

**Pros:** Closes the distribution gap; enables `pip install shop` from day one.
**Cons:** Adds ~15 min CC before any CLI code exists. Worth it.
**Context:** Design doc specifies GitHub Actions + PyInstaller. Plan has no CI/CD section. Add this alongside or immediately after first CLI command ships.
**Depends on:** First CLI command (shop search products) working.
**Effort:** S (human: ~4h / CC: ~15 min)

---

## v1 Deferrals

### Hosted discovery API (Approach B)
**What:** Move the merchant registry from a local `~/.shop/merchants.yaml` config file
to a hosted API. The API maintains merchant registrations, runs parallel UCP queries at
scale, and returns normalized results. Commercial model: API keys + usage-based pricing.

**Why:** Network effects. Every agent developer who registers a merchant makes the
registry better for all others. This is the moat and the commercial model.

**How to apply:** The v0 `merchants.yaml` schema must match the hosted API contract
(same fields, same adapter types) so v0→v1 migration is a config source swap, not a
schema rewrite. Architect toward B from day one.

**Effort:** M (human: ~1 week / CC: ~3-5 days)
**Blocked by:** v0 PMF proof.

---

### Agent identity verification (device key + request signing)
**What:** Generate an Ed25519 device key on first `shop` run. Sign outbound order
requests with the device key. Merchants can optionally verify the signature as proof
that the request came from a legitimate shop instance with a valid mandate.

**Why:** The "Know Your Agent" (KYA) problem. Agents ordering from cloud IPs look like
bots to merchant fraud detection. shop's device key is a trust signal merchants can
check. In v1, merchants who verify shop signatures could give preferential treatment
(skip CAPTCHA, faster checkout paths).

**How to apply:** v0 already generates an Ed25519 key for mandate signing. Extend it
to also sign order requests. The signing infrastructure is 90% already there.

**Effort:** S (human: ~4h / CC: ~15 min)
**Blocked by:** Merchant adoption — no point shipping until at least 1 merchant verifies it.

---

### Budget period custom anchor
**What:** Add `--period-anchor <1-28>` flag to `shop mandate create`. Lets agents set the budget reset day (e.g., payday = 15th of month) instead of always resetting on the 1st.

**Why:** Monthly budgets resetting on the 1st is a convenience default, not a policy requirement. Power users will want payday-aligned resets.

**How to apply:** `period_anchor` field in mandate YAML. Budget period start = "most recent occurrence of day N at 00:00 UTC." Handle month-end edge cases (e.g., anchor=31 for a month with 28 days → last day of month).

**Effort:** XS (human: ~1h / CC: ~10 min)
**Blocked by:** Nothing. Add after v0 ships.
