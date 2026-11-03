"""Webhook routing: verify, claim, queue, answer fast. The slow part (Claude,
Airtable, email) runs on a worker thread, because Shopify gives up on a
webhook that takes longer than 5 seconds to answer."""
from __future__ import annotations

import json
import logging
import queue
import threading
from typing import Callable, Optional, Tuple

from . import normalize
from .signatures import SignatureError, verify_shopify, verify_stripe

log = logging.getLogger("order_flow.app")

MAX_BODY = 1_000_000
STRIPE_EVENTS = {"payment_intent.succeeded", "checkout.session.completed",
                 "checkout.session.async_payment_succeeded"}


class WebhookApp:
    def __init__(self, settings, pipeline, store, stripe=None, sync: bool = False):
        self.s, self.pipeline, self.store, self.stripe = settings, pipeline, store, stripe
        self.sync = sync
        self.jobs: "queue.Queue" = queue.Queue()
        self._worker: Optional[threading.Thread] = None

    # --- worker -------------------------------------------------------------
    def start_worker(self) -> None:
        self._worker = threading.Thread(target=self._drain, daemon=True, name="order-worker")
        self._worker.start()

    def _drain(self) -> None:
        while True:
            job = self.jobs.get()
            try:
                self.pipeline.process(*job)
            finally:
                self.jobs.task_done()

    def _submit(self, event_id: str, loader: Callable, raw: bytes) -> None:
        if self.sync:
            self.pipeline.process(event_id, loader, raw)
        else:
            self.jobs.put((event_id, loader, raw))

    def replay_failed(self) -> list:
        """Re-runs every event that failed, from the body stored when it first
        arrived (its signature was checked then). Safe to repeat: Airtable is an
        upsert on Order Key and the email key is tied to the order."""
        results = []
        for item in self.store.failed():
            event = json.loads(item["body"])
            if item["source"] == "Shopify":
                loader = (lambda p: lambda: normalize.from_shopify(p))(event)
            else:
                etype, obj = event["type"], event["data"]["object"]
                loader = (lambda t, o: lambda: self._stripe_order(t, o))(etype, obj)
            results.append(self.pipeline.process(item["event_id"], loader, item["body"]))
        return results

    # --- routing ------------------------------------------------------------
    def handle(self, method: str, path: str, headers: dict, body: bytes) -> Tuple[int, dict]:
        h = {k.lower(): v for k, v in headers.items()}
        if method == "GET" and path == "/health":
            return 200, {"ok": True, "queued": self.jobs.qsize()}
        if method != "POST":
            return 405, {"error": "method not allowed"}
        if len(body) > MAX_BODY:
            return 413, {"error": "body too large"}
        if path == "/webhooks/shopify":
            return self._shopify(h, body)
        if path == "/webhooks/stripe":
            return self._stripe(h, body)
        return 404, {"error": "not found"}

    def _shopify(self, h: dict, body: bytes) -> Tuple[int, dict]:
        try:
            verify_shopify(self.s.shopify_webhook_secret, body, h.get("x-shopify-hmac-sha256"))
        except SignatureError as e:
            log.warning("rejected Shopify webhook: %s", e)
            return 401, {"error": "invalid signature"}
        if self.s.shopify_shop and h.get("x-shopify-shop-domain") not in (None, self.s.shopify_shop):
            return 403, {"error": "unknown shop"}
        topic = h.get("x-shopify-topic", "")
        if topic != "orders/paid":
            return 200, {"ignored": topic}
        payload = json.loads(body)
        order_key = f"shopify:{payload['id']}"
        event_id = "shopify:" + (h.get("x-shopify-event-id") or h.get("x-shopify-webhook-id") or order_key)
        claim = self.store.claim(event_id, order_key, "Shopify")
        if claim != "new":
            return 200, {"skipped": claim}
        self._submit(event_id, lambda: normalize.from_shopify(payload), body)
        return 200, {"queued": event_id}

    def _stripe(self, h: dict, body: bytes) -> Tuple[int, dict]:
        try:
            verify_stripe(self.s.stripe_webhook_secret, body, h.get("stripe-signature"),
                          tolerance=self.s.stripe_tolerance)
        except SignatureError as e:
            log.warning("rejected Stripe webhook: %s", e)
            return 400, {"error": "invalid signature"}
        event = json.loads(body)
        etype = event.get("type", "")
        if etype not in STRIPE_EVENTS:
            return 200, {"ignored": etype}
        obj = event["data"]["object"]
        if etype.startswith("checkout.session") and obj.get("payment_status") != "paid":
            return 200, {"ignored": "checkout session not paid yet"}
        pi_id = obj["id"] if etype == "payment_intent.succeeded" else (obj.get("payment_intent") or obj["id"])
        order_key = f"stripe:{pi_id}"
        event_id = f"stripe:{event['id']}"
        claim = self.store.claim(event_id, order_key, "Stripe")
        if claim != "new":
            return 200, {"skipped": claim}
        self._submit(event_id, lambda: self._stripe_order(etype, obj), body)
        return 200, {"queued": event_id}

    def _stripe_order(self, etype: str, obj: dict):
        charge = None
        if self.stripe:  # Radar risk outcome and card country live on the charge
            charge_id = obj.get("latest_charge")
            if not charge_id and obj.get("payment_intent"):
                charge_id = self.stripe.get_payment_intent(obj["payment_intent"]).get("latest_charge")
            if charge_id:
                charge = self.stripe.get_charge(charge_id)
        if etype == "payment_intent.succeeded":
            return normalize.from_stripe_payment_intent(obj, charge)
        return normalize.from_stripe_checkout_session(obj, charge)
