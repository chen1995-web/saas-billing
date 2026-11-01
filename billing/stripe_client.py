"""Stripe client with a demo fallback.

In production this hits the real Stripe API. In demo mode (no key) it returns
the same shapes so the whole checkout/portal flow is testable offline. Keeping
one client module means the routes never branch on "is this a demo" -- the
branch lives here, in one place.
"""

from __future__ import annotations

from dataclasses import dataclass

from config import PLANS, Settings


@dataclass
class CheckoutResult:
    session_id: str
    url: str
    plan_id: str


class StripeClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def live(self) -> bool:
        return not self.settings.demo and bool(self.settings.stripe_secret_key)

    def create_checkout_session(self, workspace_id: str, plan_id: str,
                                customer_email: str) -> CheckoutResult:
        if not self.live:
            return CheckoutResult(
                session_id=f"cs_demo_{plan_id}",
                url=f"/billing/success?plan={plan_id}",
                plan_id=plan_id,
            )
        import httpx

        plan = PLANS[plan_id]
        resp = httpx.post(
            f"{self.settings.stripe_base}/checkout/sessions",
            headers={"Authorization": f"Bearer {self.settings.stripe_secret_key}"},
            json={
                "mode": "subscription",
                "line_items": [{"price": self._price_id(plan_id), "quantity": 1}],
                "success_url": "http://localhost:8001/billing/success",
                "cancel_url": "http://localhost:8001/pricing",
                "customer_email": customer_email,
                "metadata": {"workspace_id": workspace_id, "plan_id": plan_id},
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        return CheckoutResult(data["id"], data["url"], plan_id)

    def create_portal_session(self, customer_id: str) -> str:
        if not self.live:
            return "/billing/portal-demo"
        import httpx

        resp = httpx.post(
            f"{self.settings.stripe_base}/billing/portal/sessions",
            headers={"Authorization": f"Bearer {self.settings.stripe_secret_key}"},
            json={"customer": customer_id,
                  "return_url": "http://localhost:8001/settings/billing"},
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()["url"]

    def _price_id(self, plan_id: str) -> str:
        # In real life these are Price objects looked up from the Stripe dashboard.
        return f"price_{plan_id}"
