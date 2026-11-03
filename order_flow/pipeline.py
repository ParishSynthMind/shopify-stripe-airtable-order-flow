"""One paid order, start to finish: rule check, AI review, Airtable upsert, email."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Callable

from .airtable import order_fields
from .emails import order_email
from .http import ApiError
from .models import Order
from .risk import rules_review, signals

log = logging.getLogger("order_flow.pipeline")


class OrderPipeline:
    def __init__(self, reviewer, airtable, notifier, alerter, store,
                 subject_prefix: str = "", high_value: float = 500.0,
                 retries: int = 3, backoff: float = 2.0, sleep: Callable = time.sleep):
        self.reviewer, self.airtable, self.notifier = reviewer, airtable, notifier
        self.alerter, self.store = alerter, store
        self.subject_prefix, self.high_value = subject_prefix, high_value
        self.retries, self.backoff, self._sleep = retries, backoff, sleep

    def _with_retries(self, what: str, fn: Callable):
        for attempt in range(1, self.retries + 1):
            try:
                return fn()
            except ApiError as e:
                if not e.retryable or attempt == self.retries:
                    raise
                log.warning("%s failed (%s), retry %d/%d", what, e.status, attempt, self.retries - 1)
                self._sleep(self.backoff * attempt)

    def process(self, event_id: str, load_order: Callable[[], Order], raw_body: bytes = b"") -> dict:
        """Runs after the webhook has been verified and claimed in the store."""
        try:
            order = load_order()
            found = signals(order, high_value=self.high_value)
            try:
                review = self.reviewer.review(order, found)
            except Exception as e:  # the AI being down must not lose or wave through an order
                log.warning("AI review failed for %s: %s", order.order_key, e)
                review = rules_review(order, found)
                self.alerter.alert("AI review unavailable, used rule check",
                                   f"{order.order_key} ({order.order_name}): {e}")
            received = datetime.now(timezone.utc).isoformat(timespec="seconds")
            fields = order_fields(order, review, event_id, received)
            row = self._with_retries("Airtable upsert", lambda: self.airtable.upsert_order(fields))
            subject, text = order_email(order, review, row["record_id"], self.subject_prefix)
            # The key is tied to the order, so an email provider with idempotency
            # (Resend) drops a resend of the same order after a replay.
            self.notifier.send(subject, text, key=f"order-paid:{order.order_key}")
            self.store.mark_done(event_id, row["record_id"])
            log.info("order %s done: record %s (%s), risk %s%s", order.order_key, row["record_id"],
                     "created" if row["created"] else "updated", review.risk,
                     ", FLAGGED" if review.flagged else "")
            return {"status": "done", "order": order, "review": review, **row}
        except Exception as e:
            self.store.mark_failed(event_id, f"{type(e).__name__}: {e}", raw_body)
            self.alerter.alert("order could not be processed, kept for replay",
                               f"event {event_id}: {type(e).__name__}: {e}")
            log.exception("event %s failed", event_id)
            return {"status": "failed", "error": str(e)}
