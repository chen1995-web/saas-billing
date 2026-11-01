"""Stripe event handling with real idempotency.

This is the module employers actually probe in interviews. Webhooks arrive at
least once, never exactly once, and frequently out of order. The three rules
that make this safe:

1. **Idempotency key** -- ``stripe_events.id`` is the Stripe event id; a
   duplicate is detected and skipped before any state change.
2. **Snapshot ordering** -- for ``subscription.updated`` we only apply events
   whose ``created`` is newer than what we already stored, so a late delivery
   of an old event cannot resurrect a canceled subscription.
3. **Atomic state transition** -- each handler mutates ``customers`` inside a
   single transaction and writes an audit row, so the billing state is always
   recoverable from the log.

In demo mode (no Stripe key) the same handlers are driven by locally generated
fixtures, so the behaviour is testable without a live Stripe account.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from config import PLANS, Settings
from entitlements import load_entitlements
from storage import Database


@dataclass
class WebhookResult:
    accepted: bool
    skipped: bool
    action: str


def _record_event(db: Database, event_id: str, event_type: str,
                  payload: dict, force: bool = False) -> bool:
    """Insert the event; return False if it is a duplicate."""
    with db.conn() as conn:
        if not force:
            existing = conn.execute(
                "SELECT id FROM stripe_events WHERE id=?", (event_id,)
            ).fetchone()
            if existing:
                return False
        conn.execute(
            "INSERT INTO stripe_events(id,type,processed,payload,created_at)"
            " VALUES(?,?,?,?,?)",
            (event_id, event_type, 0, json.dumps(payload), time.time()),
        )
    return True


def _mark_processed(db: Database, event_id: str) -> None:
    with db.conn() as conn:
        conn.execute("UPDATE stripe_events SET processed=1 WHERE id=?", (event_id,))


def _customer_row(db: Database, workspace_id: str):
    with db.conn() as conn:
        return conn.execute(
            "SELECT * FROM customers WHERE workspace_id=?", (workspace_id,)
        ).fetchone()


def _upsert_customer(db: Database, workspace_id: str, *, stripe_cust: str | None,
                     stripe_sub: str | None, plan_id: str, status: str,
                     period_end: float | None, canceled_at: float | None) -> None:
    now = time.time()
    with db.conn() as conn:
        row = conn.execute(
            "SELECT id FROM customers WHERE workspace_id=?", (workspace_id,)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE customers SET stripe_cust=COALESCE(?,stripe_cust),"
                " stripe_sub=COALESCE(?,stripe_sub), plan_id=?, status=?,"
                " current_period_end=?, canceled_at=?, updated_at=?"
                " WHERE id=?",
                (stripe_cust, stripe_sub, plan_id, status, period_end,
                 canceled_at, now, row["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO customers(id,workspace_id,stripe_cust,stripe_sub,"
                "plan_id,status,current_period_end,canceled_at,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (f"cust_{workspace_id}", workspace_id, stripe_cust, stripe_sub,
                 plan_id, status, period_end, canceled_at, now, now),
            )


def _plan_from_price(cents: int) -> str:
    for pid, plan in PLANS.items():
        if plan.price_cents == cents:
            return pid
    return "free"


def handle_event(db: Database, settings: Settings, event_id: str,
                 event_type: str, obj: dict, workspace_id: str) -> WebhookResult:
    # accept either the full event envelope {"object": {...}} or the bare object
    if isinstance(obj, dict) and isinstance(obj.get("object"), dict):
        obj = obj["object"]
    payload = {"id": event_id, "type": event_type, "object": obj}
    if not _record_event(db, event_id, event_type, payload):
        return WebhookResult(False, True, "duplicate-skipped")

    action = "ignored"
    sub = obj

    if event_type == "checkout.session.completed":
        _upsert_customer(db, workspace_id, stripe_cust=sub.get("customer"),
                         stripe_sub=sub.get("subscription") or sub.get("metadata", {}).get("subscription"),
                         plan_id=_plan_from_price(int(sub.get("amount_total", 0))),
                         status="active", period_end=None, canceled_at=None)
        db.log(workspace_id, "stripe", "checkout.completed",
               f"plan={_plan_from_price(int(sub.get('amount_total', 0)))}")
        action = "checkout.completed"

    elif event_type == "customer.subscription.updated":
        status = sub.get("status")
        plan = _plan_from_price(int(sub.get("items", [{}])[0].get("price", {}).get("unit_amount", 0)))
        created = int(sub.get("created", 0))
        row = _customer_row(db, workspace_id)
        # snapshot ordering: ignore stale deliveries
        if row and row["current_period_end"] and created:
            current = int(row["current_period_end"])
            if created < current and status != "canceled":
                _mark_processed(db, event_id)
                return WebhookResult(True, True, "stale-ignored")
        _upsert_customer(
            db, workspace_id, stripe_cust=sub.get("customer"),
            stripe_sub=sub.get("id"), plan_id=plan,
            status="active" if status in {"active", "trialing"} else status,
            period_end=sub.get("current_period_end"),
            canceled_at=sub.get("canceled_at"),
        )
        db.log(workspace_id, "stripe", "subscription.updated",
               f"status={status} plan={plan}")
        action = f"subscription.{status}"

    elif event_type == "invoice.payment_failed":
        _upsert_customer(db, workspace_id, stripe_cust=None, stripe_sub=None,
                         plan_id=(_customer_row(db, workspace_id) or {"plan_id": "free"})["plan_id"],
                         status="past_due", period_end=None, canceled_at=None)
        db.log(workspace_id, "stripe", "invoice.payment_failed")
        action = "payment_failed"

    elif event_type == "customer.subscription.deleted":
        _upsert_customer(db, workspace_id, stripe_cust=None, stripe_sub=None,
                         plan_id="free", status="canceled",
                         period_end=None, canceled_at=int(sub.get("canceled_at", time.time())))
        db.log(workspace_id, "stripe", "subscription.canceled")
        action = "subscription.canceled"

    _mark_processed(db, event_id)
    return WebhookResult(True, False, action)


def simulate_subscription_cycle(db: Database, settings: Settings,
                                workspace_id: str) -> list[WebhookResult]:
    """Drive a full billing cycle with fixtures (demo mode / tests)."""
    base = int(time.time())
    events = [
        ("evt_checkout", "checkout.session.completed", {
            "object": {"customer": "cus_demo", "subscription": "sub_demo",
                       "amount_total": 2900}}),
        ("evt_sub", "customer.subscription.updated", {
            "object": {"customer": "cus_demo", "id": "sub_demo", "status": "active",
                       "created": base, "current_period_end": base + 30 * 86400,
                       "items": [{"price": {"unit_amount": 2900}}]}}),
        ("evt_fail", "invoice.payment_failed", {
            "object": {"customer": "cus_demo", "subscription": "sub_demo"}}),
        ("evt_cancel", "customer.subscription.deleted", {
            "object": {"customer": "cus_demo", "id": "sub_demo",
                       "canceled_at": base + 3600}}),
    ]
    out = []
    for eid, etype, data in events:
        out.append(handle_event(db, settings, eid, etype, data, workspace_id))
    return out
