# SaaS Billing Starter

A self-contained, demo-runnable billing backend for a multi-tenant SaaS: plans,
checkout, webhook-driven subscription state, and entitlement gating — with a
pricing page that walks through the full billing loop without any Stripe
account. Built to be read in one sitting: the interesting parts (idempotency,
lifecycle transitions, gating) are each a single, commented function.

## What it does

- **Webhook intake with real idempotency.** `POST /webhooks/stripe` accepts
  Stripe events; duplicate event ids are rejected by the `stripe_events.id`
  primary key before any state change, so at-least-once delivery never
  double-charges or re-activates.
- **Subscription lifecycle.** `checkout.session.completed`,
  `customer.subscription.updated`, `invoice.payment_failed` and
  `customer.subscription.deleted` move a workspace through
  free → paid → past_due → canceled, with every transition audited.
- **Entitlement gating.** One `can(workspace, feature)` function is the single
  source of truth for "can this workspace do X" (SSO, SLA, audit log, seats).
  `POST /api/usage` blocks gated actions with HTTP 403 — gating is enforced,
  not decorative.
- **Pricing page demo.** `static/pricing.html` renders the plan grid from
  `/api/plans`, and clicking a plan runs a real checkout request, simulates the
  webhook Stripe would deliver, and shows the resulting entitlements — the
  whole money loop on one page.
- **Simulated billing cycle.** `POST /api/simulate/cycle` drives a full
  checkout → active → payment-failed → canceled sequence with fixtures, so the
  lifecycle is testable offline.

## Tech stack

- Python 3.11, FastAPI + uvicorn
- SQLite (WAL mode), stdlib `sqlite3` — no ORM, schema in
  [`storage.py`](storage.py) is close to a real multi-tenant schema
  (users → memberships → workspaces → customers)
- Stripe API client in [`billing/stripe_client.py`](billing/stripe_client.py)
  with a demo fallback that returns the same shapes without a network
- Vanilla JS single page, no build step
- i18n on the pricing page: English default, Chinese toggle, no dependencies

## Quick start

Demo mode is on by default — no Stripe key, no network calls.

```bash
pip install -r requirements.txt
python app.py            # serves on http://127.0.0.1:8001
```

Open <http://127.0.0.1:8001/> for the pricing page. A demo workspace
(`ws_demo`) is seeded at startup; the page talks to it via the `X-Workspace`
header. Set `STRIPE_SECRET_KEY` (and `STRIPE_WEBHOOK_SECRET`) to switch to live
Stripe mode; `BILL_DEMO=1` forces demo mode.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/plans` | Plan catalog with prices, seats, storage, features |
| POST | `/api/subscribe` | Create a checkout session for `{workspace_id, plan_id, email}` |
| GET | `/api/entitlements` | Current plan, status, seats and per-feature flags (`X-Workspace` header) |
| POST | `/webhooks/stripe` | Stripe event intake — idempotent, signature-free in demo mode |
| POST | `/api/usage` | Record a usage event; gated actions (`sso_login`, `export_full_report`) return 403 unless entitled |
| GET | `/api/audit` | Recent billing audit log |
| POST | `/api/simulate/cycle` | Drive a full billing cycle with fixtures |
| POST | `/api/workspaces` | Create a workspace (demo auth via `X-User` header) |

## Tests

```bash
python -m pytest tests/ -q          # add -p no:asyncio if pytest-asyncio is broken in your env
```

5 tests, all offline (temp SQLite DBs, no network):

1. `test_duplicate_webhook_is_skipped` — replaying the same event id is
   skipped and the plan is not re-applied.
2. `test_stale_subscription_event_ignored` — a late-delivered old
   `subscription.updated` cannot downgrade a newer plan.
3. `test_full_lifecycle_ends_canceled_free` — checkout → active →
   payment_failed → canceled leaves the workspace on free/canceled.
4. `test_entitlement_gating_blocks_sso_on_free` — free workspace has no SSO;
   promoting to team grants SSO but not SLA.
5. `test_plan_prices_are_monotonic` — plan prices increase with tier.

## i18n

The pricing page ships with a tiny zero-dependency i18n layer: an
`I18N = { en: {...}, zh: {...} }` dictionary, a `t(key, vars)` helper, and an
EN / 中文 toggle in the header. State persists in `localStorage` (key `lang`),
defaults to English, and `<html lang>` + `<title>` follow the switch. All
user-visible copy goes through `t()` — including dynamically generated text:
plan names (`Pro` → `专业版`), the feature matrix, and the status lines that
report checkout sessions, webhook results, and current entitlements. The
backend stays English-only (API strings, plan ids, and error details), so the
frontend maps display labels without touching the data layer.

## Design notes

- **Idempotency by key, not by luck.** The event id is the primary key of
  `stripe_events`; the INSERT itself is the dedup gate, so a duplicate is
  rejected atomically before any handler logic runs.
- **Snapshot ordering prevents downgrades.** For `subscription.updated`,
  events whose `created` timestamp is older than the stored
  `current_period_end` are ignored — a resurrected old webhook can't undo a
  cancellation or a plan change.
- **Atomic state transitions.** Each handler mutates `customers` inside one
  `sqlite3` transaction and writes an audit row in the same pass; the billing
  state is always recoverable from the log.
- **One `can()`.** Every feature gate in the app resolves through
  `entitlements.can()`; changing plan semantics means changing one file, not
  eight handlers.
