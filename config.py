"""SaaS billing starter -- config."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Plan:
    id: str
    name: str
    price_cents: int          # per month
    seats: int
    storage_mb: int
    features: list[str]


PLANS: dict[str, Plan] = {
    "free": Plan("free", "Free", 0, 1, 100, ["1 workspace", "1 seat"]),
    "pro": Plan("pro", "Pro", 2900, 5, 10000, ["5 seats", "10 GB", "audit log"]),
    "team": Plan("team", "Team", 9900, 20, 100000, ["20 seats", "100 GB", "SSO", "audit log"]),
    "scale": Plan("scale", "Scale", 29900, 100, 1000000, ["100 seats", "1 TB", "SSO", "SLA"]),
}


@dataclass
class Settings:
    db_path: str = "data/billing.sqlite3"
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_base: str = "https://api.stripe.com/v1"
    # demo mode: no Stripe calls, webhooks simulated locally
    demo: bool = True
    port: int = 8001


def load() -> Settings:
    s = Settings()
    s.db_path = _env("BILL_DB", s.db_path)
    s.stripe_secret_key = _env("STRIPE_SECRET_KEY")
    s.stripe_webhook_secret = _env("STRIPE_WEBHOOK_SECRET")
    s.stripe_base = _env("STRIPE_BASE", s.stripe_base)
    s.demo = _env_bool("BILL_DEMO", not s.stripe_secret_key)
    s.port = int(_env("BILL_PORT", str(s.port)) or s.port)
    return s
