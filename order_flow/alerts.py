"""Failure path. An order that cannot be written is never dropped quietly:
it is kept in the event store for replay and an alert goes out."""
from __future__ import annotations

import logging
from typing import Callable, Optional

from .http import request

log = logging.getLogger("order_flow.alerts")


class Alerter:
    """Sends to a Slack-style incoming webhook if ALERT_WEBHOOK_URL is set, and
    through the email notifier if one is given. Logs always. Never raises."""

    def __init__(self, webhook_url: str = "", notifier=None, prefix: str = "",
                 send: Optional[Callable] = None):
        self.webhook_url, self.notifier, self.prefix = webhook_url, notifier, prefix
        self._send = send or request
        self.sent = []

    def alert(self, title: str, detail: str) -> None:
        text = f"{self.prefix}ORDER FLOW ALERT: {title}"
        log.error("%s | %s", text, detail)
        self.sent.append((title, detail))
        if self.webhook_url:
            try:
                self._send("Alert", "POST", self.webhook_url, {}, json_body={"text": f"{text}\n{detail}"})
            except Exception as e:  # an alert failing must not hide the original problem
                log.error("alert webhook failed: %s", e)
        if self.notifier:
            try:
                self.notifier.send(text, detail, key=f"alert:{abs(hash((title, detail)))}")
            except Exception as e:
                log.error("alert email failed: %s", e)
