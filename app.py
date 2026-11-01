"""FastAPI app for the SaaS billing starter.

Endpoints:
  GET  /pricing                  marketing pricing page
  POST /api/workspaces          create workspace (demo auth)
  POST /api/subscribe            start a checkout session
  GET  /api/entitlements          what this workspace can do
  POST /api/usage                record a feature usage event (gated)
  POST /webhooks/stripe          Stripe event intake (idempotent)
  GET  /api/audit                recent billing audit log
  POST /api/simulate/cycle       drive a full billing cycle with fixtures

Run:  python app.py  (demo mode by default)
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from billing.stripe_client import StripeClient  # noqa: E402
from billing.webhook import handle_event, simulate_subscription_cycle  # noqa: E402
from config import PLANS, load  # noqa: E402
from entitlements import can, load_entitlements  # noqa: E402
from storage import Database  # noqa: E402

app = FastAPI(title="SaaS Billing Starter", version="1.0.0")
STATIC = os.path.join(ROOT, "static")


def db() -> Database:
    return Database(load().db_path)


def seed_demo() -> None:
    """Ensure the demo workspace exists so the pricing page works out of the box."""
    d = db()
    with d.conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users(id,email,name,created_at) VALUES(?,?,?,?)",
            ("demo-user", "demo@example.com", "Demo", time.time()),
        )
        conn.execute(
            "INSERT OR IGNORE INTO workspaces(id,name,owner_id,created_at) VALUES(?,?,?,?)",
            ("ws_demo", "Demo Workspace", "demo-user", time.time()),
        )
        conn.execute(
            "INSERT OR IGNORE INTO memberships(id,workspace_id,user_id,role,created_at)"
            " VALUES(?,?,?,?,?)",
            ("mem_demo", "ws_demo", "demo-user", "owner", time.time()),
        )
        conn.execute(
            "INSERT OR IGNORE INTO customers(id,workspace_id,stripe_cust,stripe_sub,"
            "plan_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ("cust_ws_demo", "ws_demo", None, None, "free", "active", time.time(), time.time()),
        )


# --- demo auth: X-User header instead of real auth, to keep it zero-dependency
def current_user(request: Request) -> str:
    return request.headers.get("X-User", "demo-user")


def current_workspace(request: Request) -> str:
    return request.headers.get("X-Workspace", "ws_demo")


@app.get("/")
def pricing_page() -> FileResponse:
    return FileResponse(os.path.join(STATIC, "pricing.html"))


@app.get("/api/plans")
def plans() -> dict:
    return {"plans": [vars(p) for p in PLANS.values()]}


class WorkspaceBody(BaseModel):
    name: str


@app.post("/api/workspaces")
def create_workspace(request: Request, body: WorkspaceBody) -> dict:
    user = current_user(request)
    wid = f"ws_{uuid.uuid4().hex[:8]}"
    d = db()
    with d.conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users(id,email,name,created_at) VALUES(?,?,?,?)",
            (user, f"{user}@example.com", body.name, time.time()),
        )
        conn.execute(
            "INSERT INTO workspaces(id,name,owner_id,created_at) VALUES(?,?,?,?)",
            (wid, body.name, user, time.time()),
        )
        conn.execute(
            "INSERT INTO memberships(id,workspace_id,user_id,role,created_at)"
            " VALUES(?,?,?,?,?)",
            (f"mem_{uuid.uuid4().hex[:8]}", wid, user, "owner", time.time()),
        )
        conn.execute(
            "INSERT INTO customers(id,workspace_id,stripe_cust,stripe_sub,plan_id,status,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (f"cust_{wid}", wid, None, None, "free", "active", time.time(), time.time()),
        )
    d.log(wid, user, "workspace.created")
    return {"workspace_id": wid, "name": body.name}


class SubscribeBody(BaseModel):
    workspace_id: str
    plan_id: str
    email: str = "customer@example.com"


@app.post("/api/subscribe")
def subscribe(request: Request, body: SubscribeBody) -> dict:
    if body.plan_id not in PLANS:
        raise HTTPException(400, "unknown plan")
    client = StripeClient(load())
    result = client.create_checkout_session(body.workspace_id, body.plan_id, body.email)
    db().log(body.workspace_id, current_user(request), "checkout.started",
             f"plan={body.plan_id}")
    return {"session_id": result.session_id, "url": result.url, "plan": body.plan_id}


@app.get("/api/entitlements")
def entitlements(request: Request) -> dict:
    wid = current_workspace(request)
    ent = load_entitlements(db(), wid)
    return {
        "workspace_id": wid,
        "plan": ent.plan_id,
        "status": ent.status,
        "seats": ent.seats_available(),
        "storage_mb": ent.storage_mb,
        "features": {f: ent.has(f) for f in
                     ["sso", "sla", "audit_log", "1 workspace", "5 seats"]},
    }


class UsageBody(BaseModel):
    workspace_id: str
    action: str
    quantity: int = 1


@app.post("/api/usage")
def usage(request: Request, body: UsageBody) -> dict:
    wid = body.workspace_id
    d = db()
    # gate a premium action to prove entitlements are enforced, not decorative
    if body.action in {"sso_login", "export_full_report"} and not can(d, wid, "sso"):
        raise HTTPException(403, "Your plan does not include this feature")
    with d.conn() as conn:
        conn.execute(
            "INSERT INTO usage_events(workspace_id,user_id,action,quantity,created_at)"
            " VALUES(?,?,?,?,?)",
            (wid, current_user(request), body.action, body.quantity, time.time()),
        )
    return {"recorded": body.action, "quantity": body.quantity}


@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request) -> JSONResponse:
    payload = await request.body()
    try:
        data = json_loads(payload)
    except Exception:
        raise HTTPException(400, "invalid JSON")
    event_id = data.get("id", f"evt_{uuid.uuid4().hex[:8]}")
    event_type = data.get("type", "")
    obj = data.get("data", {}).get("object", {})
    ws = (obj.get("metadata") or {}).get("workspace_id") or request.headers.get("X-Workspace", "ws_demo")
    result = handle_event(db(), load(), event_id, event_type, obj, ws)
    return JSONResponse({"received": True, "action": result.action,
                         "skipped": result.skipped})


@app.get("/api/audit")
def audit(limit: int = 20) -> dict:
    with db().conn() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return {"events": [dict(r) for r in rows]}


@app.post("/api/simulate/cycle")
def simulate_cycle(request: Request) -> dict:
    wid = current_workspace(request)
    results = simulate_subscription_cycle(db(), load(), wid)
    ent = load_entitlements(db(), wid)
    return {
        "actions": [r.action for r in results],
        "final_plan": ent.plan_id,
        "final_status": ent.status,
    }


def json_loads(raw: bytes) -> dict:
    import json
    return json.loads(raw)


app.mount("/static", StaticFiles(directory=STATIC), name="static")

seed_demo()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=load().port)
