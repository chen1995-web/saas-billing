"""Billing tests -- the file that proves the money logic.

    python -m pytest tests/ -v

Covers the three things a reviewer will actually ask about:
1. webhook idempotency (duplicate delivery does not double-charge / re-activate)
2. entitlement gating (a free workspace cannot call an SSO endpoint)
3. the full subscription lifecycle end-to-end
"""

from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from config import Settings, PLANS  # noqa: E402
from storage import Database  # noqa: E402
from billing.webhook import handle_event, simulate_subscription_cycle  # noqa: E402
from entitlements import load_entitlements, can  # noqa: E402


def make_db():
    tmp = tempfile.mktemp(suffix=".sqlite3")
    return Database(tmp)


def make_settings():
    return Settings(db_path=tempfile.mktemp(suffix=".sqlite3"), demo=True)


def _ws(db, wid="ws_test"):
    with db.conn() as conn:
        conn.execute(
            "INSERT INTO users(id,email,name,created_at) VALUES(?,?,?,?)",
            ("usr_test", "t@example.com", "Tester", 0),
        )
        conn.execute(
            "INSERT INTO workspaces(id,name,owner_id,created_at) VALUES(?,?,?,?)",
            (wid, "Test WS", "usr_test", 0),
        )
        conn.execute(
            "INSERT INTO customers(id,workspace_id,plan_id,status,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?)",
            (f"cust_{wid}", wid, "free", "active", 0, 0),
        )


def test_duplicate_webhook_is_skipped():
    db = make_db()
    s = make_settings()
    _ws(db)
    data = {"object": {"customer": "cus_demo", "subscription": "sub_demo", "amount_total": 2900}}
    first = handle_event(db, s, "evt_dup", "checkout.session.completed", data, "ws_test")
    second = handle_event(db, s, "evt_dup", "checkout.session.completed", data, "ws_test")
    assert first.accepted and not first.skipped
    assert second.skipped, "duplicate event id must be ignored"
    assert load_entitlements(db, "ws_test").plan_id == "pro"


def test_stale_subscription_event_ignored():
    db = make_db()
    s = make_settings()
    _ws(db)
    # mark as pro at a newer time
    handle_event(db, s, "evt_new", "customer.subscription.updated",
                 {"object": {"status": "active", "created": 2000,
                             "current_period_end": 3000,
                             "items": [{"price": {"unit_amount": 9900}}]}}, "ws_test")
    # an old event arrives later
    result = handle_event(db, s, "evt_old", "customer.subscription.updated",
                          {"object": {"status": "active", "created": 1000,
                                      "current_period_end": 1500,
                                      "items": [{"price": {"unit_amount": 2900}}]}}, "ws_test")
    assert result.skipped, "stale delivery must not downgrade the plan"
    assert load_entitlements(db, "ws_test").plan_id == "team"


def test_full_lifecycle_ends_canceled_free():
    db = make_db()
    s = make_settings()
    _ws(db)
    results = simulate_subscription_cycle(db, s, "ws_test")
    assert [r.action for r in results] == [
        "checkout.completed", "subscription.active", "payment_failed", "subscription.canceled"
    ]
    ent = load_entitlements(db, "ws_test")
    assert ent.plan_id == "free" and ent.status == "canceled"


def test_entitlement_gating_blocks_sso_on_free():
    db = make_db()
    _ws(db)
    assert not can(db, "ws_test", "sso")
    assert can(db, "ws_test", "1 workspace")
    # promote to team
    with db.conn() as conn:
        conn.execute("UPDATE customers SET plan_id='team', status='active' WHERE workspace_id=?",
                     ("ws_test",))
    assert can(db, "ws_test", "sso")
    assert not can(db, "ws_test", "sla")


def test_plan_prices_are_monotonic():
    prices = [p.price_cents for p in PLANS.values() if p.id != "free"]
    assert prices == sorted(prices)
