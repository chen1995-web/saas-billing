"""Entitlements -- the single source of truth for "can this workspace do X".

The point of this module is that billing logic should never be scattered across
route handlers. Every feature gate in the app calls ``can()``; when the plan
changes, only this file changes. That is the difference between a SaaS that
scales and a SaaS where the Pro/Team/Scale wall is implemented differently in
eight places and leaks for four months.
"""

from __future__ import annotations

from dataclasses import dataclass

from config import PLANS
from storage import Database


@dataclass
class Entitlements:
    workspace_id: str
    plan_id: str
    status: str
    seats: int
    storage_mb: int

    def has(self, feature: str) -> bool:
        plan = PLANS.get(self.plan_id)
        if not plan:
            return False
        graded = {
            "sso": {"team", "scale"},
            "sla": {"scale"},
            "audit_log": {"pro", "team", "scale"},
        }
        included = feature in plan.features or (
            feature in graded and self.plan_id in graded[feature])
        if not included:
            return False
        # free-plan features are always on; paid features require active status
        if self.plan_id == "free":
            return True
        return self.status == "active"

    def seats_available(self) -> int:
        return self.seats if self.status == "active" else 0


def load_entitlements(db: Database, workspace_id: str) -> Entitlements:
    with db.conn() as conn:
        row = conn.execute(
            "SELECT c.plan_id, c.status FROM customers c"
            " WHERE c.workspace_id=? ORDER BY c.created_at DESC LIMIT 1",
            (workspace_id,),
        ).fetchone()
    if not row:
        return Entitlements(workspace_id, "free", "active", 1, 100)
    plan = PLANS.get(row["plan_id"], PLANS["free"])
    return Entitlements(workspace_id, row["plan_id"], row["status"],
                        plan.seats, plan.storage_mb)


def can(db: Database, workspace_id: str, feature: str) -> bool:
    return load_entitlements(db, workspace_id).has(feature)
