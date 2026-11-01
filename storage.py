"""Storage schema for the billing starter.

The schema is deliberately close to what a real multi-tenant SaaS needs:
users belong to workspaces, memberships carry roles, subscriptions are tied to
a customer record in Stripe, and every state change is auditable.
"""

from __future__ import annotations

import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id         TEXT PRIMARY KEY,
    email      TEXT UNIQUE NOT NULL,
    name       TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS workspaces (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    owner_id   TEXT NOT NULL REFERENCES users(id),
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS memberships (
    id          TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role        TEXT NOT NULL DEFAULT 'member',
    created_at  REAL NOT NULL,
    UNIQUE(workspace_id, user_id)
);
CREATE TABLE IF NOT EXISTS customers (
    id            TEXT PRIMARY KEY,
    workspace_id  TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    stripe_cust   TEXT,
    stripe_sub    TEXT,
    plan_id       TEXT NOT NULL DEFAULT 'free',
    status        TEXT NOT NULL DEFAULT 'active',
    current_period_end REAL,
    canceled_at   REAL,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id     TEXT NOT NULL,
    action      TEXT NOT NULL,
    quantity    INTEGER NOT NULL DEFAULT 1,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS stripe_events (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    processed   INTEGER NOT NULL DEFAULT 0,
    payload     TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id TEXT,
    actor       TEXT,
    action      TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cust_ws ON customers(workspace_id);
CREATE INDEX IF NOT EXISTS idx_usage_ws ON usage_events(workspace_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_type ON stripe_events(type, processed);
"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with self.conn() as db:
            db.executescript(SCHEMA)

    def conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def log(self, workspace_id: str | None, actor: str, action: str,
            detail: str = "") -> None:
        with self.conn() as db:
            db.execute(
                "INSERT INTO audit_logs(workspace_id,actor,action,detail,created_at)"
                " VALUES(?,?,?,?,?)",
                (workspace_id, actor, action, detail, time.time()),
            )
