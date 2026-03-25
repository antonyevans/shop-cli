# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Status

**shop-cli is currently in design/spec stage — no implementation exists yet.** The repository contains only research documents in `research/`. The canonical plan is `research/2026-03-25-ceo-plan-shop-cli-v0.md` (supersedes the original design doc for all architectural decisions).

## What This Project Is

`shop` is a CLI built with the AI agent as the primary user. Humans configure it once; agents invoke it autonomously. It follows a "callable primitive" model for agentic commerce.

**Stack (decided):** Python 3.12, typer, pydantic v2, httpx (async), sqlite3, cryptography (Ed25519), python-jcs (RFC 8785).

**Payment backend (v0):** StripeDemoAdapter (self-hosted demo server, Stripe test mode). No real PCI scope in v0. AP2 SPT vaulting is v1.

**Build first:** `shop search products` and `shop merchant add` — prove cross-merchant search before building mandate/cart/order. See TODOS.md phase milestone.

## Architecture

**Command structure:** `shop <noun> <verb> [flags]` — JSON always, no `--output json` flag needed.

| Noun | Purpose |
|------|---------|
| `search` | Product discovery (UCP adapters, parallel async) |
| `product` | Product detail |
| `cart` | Cart management (supports `--dry-run`) |
| `order` | Create/track orders |
| `mandate` | Spending authority (Ed25519-signed local files) |
| `merchant` | Register UCP-compatible merchants |
| `history` | Transaction audit log (SQLite) |
| `schema` | Runtime self-description for agent introspection |

**`approval` noun is v1** — not implemented in v0. Exit 5 fires for low confidence but writes no queue.

**Mandates** are Ed25519-signed local YAML files specifying budget caps, category allow-lists, per-order limits. Agents never see card credentials. Every order must reference a mandate. v0 mandate enforcement is local-only — merchants do not verify mandates in v0.

**CommerceTXT** is shop's internal normalized JSON format. It is NOT a merchant-published format. Merchants implement UCP; shop normalizes UCP responses to CommerceTXT internally.

**Exit codes** are semantic — agents branch on them:
- `0` success, `1` bad args, `2` auth error, `3` mandate violation, `4` unavailable/checkout_not_supported
- `5` low confidence (no queue write in v0), `6` network/DB error (retry-safe), `10` NOT REACHABLE in v0

**Config:** `~/.shop/config.yaml` — default mandate, confidence threshold (default 0.80), max_workers.

## Core Design Principles

1. All commands return JSON — no `--output` flag needed, JSON is always the output
2. All mutations take an `--idempotency-key` — safe to retry
3. Agents never block — uncertain purchases exit 5, not failures. Approval queue is v1.
4. Policy lives in mandates, not agent logic
5. `schema` noun enables runtime capability discovery — `shop schema commands`

## gstack

Use the `/browse` skill from gstack for all web browsing. Never use `mcp__claude-in-chrome__*` tools.

**Output directory:** When any gstack skill writes output files (design docs, reports, investigation notes, retro snapshots, or any other artifact), write them to `research/` instead of `.context/`. The `.context/` directory is a symlink to `research/` so shell-based writes land there automatically.

Available skills: /office-hours, /plan-ceo-review, /plan-eng-review, /plan-design-review, /design-consultation, /review, /ship, /land-and-deploy, /canary, /benchmark, /browse, /qa, /qa-only, /design-review, /setup-browser-cookies, /setup-deploy, /retro, /investigate, /document-release, /codex, /cso, /autoplan, /careful, /freeze, /guard, /unfreeze, /gstack-upgrade

If gstack skills aren't working, run `cd .claude/skills/gstack && ./setup` to rebuild the binary and register skills.

## Open Questions

- Multi-agent mandate sharing / delegation hierarchy (v1)
- Mandate revocation propagation across in-flight sessions (v1)
- MCP server wrapper — v1 distribution channel (CLI is primary; MCP is thin adapter over CLI)
- ACP vs AP2 — build adapters for both in v1; v0 uses local mandate files only
