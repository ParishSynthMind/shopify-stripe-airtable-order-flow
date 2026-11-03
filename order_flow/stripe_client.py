"""The few Stripe calls this needs, on the standard library."""
from __future__ import annotations

from typing import Callable, Optional

from .http import request

API = "https://api.stripe.com/v1"


class StripeClient:
    def __init__(self, secret_key: str, send: Optional[Callable] = None):
        self.secret_key = secret_key
        self._send = send or request

    def _headers(self, idempotency_key: Optional[str] = None) -> dict:
        h = {"Authorization": f"Bearer {self.secret_key}"}
        if idempotency_key:
            h["Idempotency-Key"] = idempotency_key
        return h

    def get_charge(self, charge_id: str) -> dict:
        """The charge carries Radar's risk outcome and the card's country."""
        return self._send("Stripe", "GET", f"{API}/charges/{charge_id}", self._headers())

    def get_payment_intent(self, pi_id: str) -> dict:
        return self._send("Stripe", "GET", f"{API}/payment_intents/{pi_id}", self._headers())

    def create_webhook_endpoint(self, url: str, events: list) -> dict:
        form = {"url": url, "enabled_events[]": events,
                "description": "shopify-stripe-airtable-order-flow"}
        return self._send("Stripe", "POST", f"{API}/webhook_endpoints", self._headers(), form=form)

    def list_webhook_endpoints(self) -> list:
        return self._send("Stripe", "GET", f"{API}/webhook_endpoints", self._headers()).get("data", [])

    def delete_webhook_endpoint(self, endpoint_id: str) -> dict:
        return self._send("Stripe", "DELETE", f"{API}/webhook_endpoints/{endpoint_id}", self._headers())

    def create_test_payment(self, amount_minor: int, currency: str, payment_method: str,
                            metadata: dict, receipt_email: str, shipping: dict,
                            idempotency_key: Optional[str] = None) -> dict:
        """Creates and confirms a PaymentIntent with a Stripe test payment method
        (for example pm_card_visa). Test mode keys only."""
        if not self.secret_key.startswith("sk_test_"):
            raise RuntimeError("refusing to create a payment with a non-test key")
        form = {
            "amount": amount_minor, "currency": currency, "confirm": "true",
            "payment_method": payment_method,
            # card test methods only, so no redirect-based methods are offered
            "automatic_payment_methods[enabled]": "true",
            "automatic_payment_methods[allow_redirects]": "never",
            "receipt_email": receipt_email,
            "shipping[name]": shipping["name"],
            "shipping[address][line1]": shipping["line1"],
            "shipping[address][city]": shipping["city"],
            "shipping[address][country]": shipping["country"],
        }
        for k, v in metadata.items():
            form[f"metadata[{k}]"] = v
        return self._send("Stripe", "POST", f"{API}/payment_intents", self._headers(idempotency_key), form=form)
