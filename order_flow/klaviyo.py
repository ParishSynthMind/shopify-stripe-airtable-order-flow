"""Klaviyo: the customer as a profile, and a 'Placed Order' event per paid
order, so a Klaviyo flow (a thank-you email, a review request) can start by
itself. Expired Stripe Checkout sessions become 'Started Checkout' events,
which is what Klaviyo's abandoned-checkout flows listen for.

Consent: this only writes profiles and events. It never calls Klaviyo's
subscribe endpoints and never sets marketing consent, so nobody is opted in
to marketing by this code. Opt-in stays with Klaviyo's own sign-up forms.

Idempotency: every event carries a unique_id tied to the order (not to the
webhook delivery), so a retried webhook, a replay, or Stripe sending two
events for one payment all land as one event in Klaviyo.
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Callable, List, Optional, Tuple

from .models import Order

log = logging.getLogger("order_flow.klaviyo")

API = "https://a.klaviyo.com/api"
DEFAULT_REVISION = "2026-10-15"
USER_AGENT = "shopify-stripe-airtable-order-flow/1.1 (klaviyo-sync)"

PLACED_ORDER = "Placed Order"
STARTED_CHECKOUT = "Started Checkout"

EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                      r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")
_EMAIL_ANYWHERE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
MAX_ITEMS = 100
MAX_TEXT = 255


class KlaviyoError(Exception):
    """A readable Klaviyo failure. The message never contains an email
    address or the API key, so it is safe to log and to put in an alert."""

    def __init__(self, status: int, message: str):
        super().__init__(f"Klaviyo {status}: {message}")
        self.status = status

    @property
    def retryable(self) -> bool:
        return self.status == 429 or self.status >= 500


class ValidationError(ValueError):
    """The order cannot be sent to Klaviyo as it is (bad email, bad amount...)."""


def redact(text: str) -> str:
    return _EMAIL_ANYWHERE.sub("<email>", text or "")


def readable_error(status: int, raw: bytes) -> str:
    """JSON:API errors -> 'code: detail (at /pointer)', emails removed."""
    try:
        errors = json.loads(raw or b"{}").get("errors") or []
    except ValueError:
        errors = []
    parts = []
    for e in errors[:3]:
        where = (e.get("source") or {}).get("pointer")
        text = f"{e.get('code') or e.get('title') or 'error'}: {e.get('detail') or e.get('title') or ''}".strip()
        parts.append(text + (f" (at {where})" if where else ""))
    if not parts:
        parts = [raw.decode(errors="replace")[:200] if raw else "no response body"]
    return redact("; ".join(parts))


# --- HTTP ---------------------------------------------------------------------

def urllib_transport(method: str, url: str, headers: dict, body: Optional[bytes],
                     timeout: float) -> Tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()
    except urllib.error.URLError as e:
        return 599, {}, json.dumps({"errors": [{"code": "network", "detail": str(e.reason)}]}).encode()


class KlaviyoClient:
    """Klaviyo's JSON:API with a private key. Retries 429 using Klaviyo's
    Retry-After header and 5xx/network errors with backoff; any other 4xx is
    raised straight away with Klaviyo's own error text."""

    def __init__(self, api_key: str, revision: str = DEFAULT_REVISION, dry_run: bool = False,
                 transport: Optional[Callable] = None, sleep: Callable = time.sleep,
                 max_attempts: int = 4, backoff: float = 1.0, max_wait: float = 60.0, timeout: float = 20):
        if not dry_run and not api_key:
            raise ValueError("KLAVIYO_PRIVATE_API_KEY is not set (or turn on KLAVIYO_DRY_RUN)")
        self._key = api_key
        self.revision, self.dry_run = revision, dry_run
        self._transport = transport or urllib_transport
        self._sleep = sleep
        self.max_attempts, self.backoff, self.max_wait, self.timeout = max_attempts, backoff, max_wait, timeout
        self.dry_run_log: List[dict] = []

    def __repr__(self) -> str:  # never show the key, even in a traceback or debugger
        return f"KlaviyoClient(revision={self.revision!r}, dry_run={self.dry_run})"

    def _headers(self) -> dict:
        return {"Authorization": f"Klaviyo-API-Key {self._key}", "revision": self.revision,
                "accept": "application/vnd.api+json", "content-type": "application/vnd.api+json",
                "User-Agent": USER_AGENT}

    def call(self, method: str, path: str, body: Optional[dict] = None) -> Tuple[int, dict]:
        if self.dry_run and method != "GET":
            self.dry_run_log.append({"method": method, "path": path, "body": body})
            log.info("dry run: %s %s not sent", method, path)
            return 0, {}
        url = path if path.startswith("http") else API + path
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(1, self.max_attempts + 1):
            status, headers, raw = self._transport(method, url, self._headers(), data, self.timeout)
            if status < 300:
                return status, (json.loads(raw) if raw else {})
            err = KlaviyoError(status, readable_error(status, raw))
            if not err.retryable or attempt == self.max_attempts:
                raise err
            wait = self._retry_after(headers) if status == 429 else None
            wait = self.backoff * 2 ** (attempt - 1) if wait is None else wait
            log.warning("Klaviyo %s %s -> %s, waiting %.1fs (attempt %d/%d)", method, path.split("?")[0],
                        status, wait, attempt, self.max_attempts)
            self._sleep(wait)
        raise AssertionError("unreachable")

    def _retry_after(self, headers: dict) -> Optional[float]:
        value = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
        try:
            return min(max(float(value), 0.0), self.max_wait) if value is not None else None
        except ValueError:
            return None

    # --- the two writes this project makes -----------------------------------
    def upsert_profile(self, profile_attributes: dict) -> Optional[str]:
        """POST /profile-import creates or updates by email. Returns the profile id
        (None in dry-run). Klaviyo answers 201 when created, 200 when updated."""
        _, resp = self.call("POST", "/profile-import",
                            {"data": {"type": "profile", "attributes": profile_attributes}})
        return (resp.get("data") or {}).get("id")

    def create_event(self, event_attributes: dict) -> None:
        """POST /events. Klaviyo answers 202 and records it shortly after; an event
        whose unique_id it has already seen for that profile and metric is dropped."""
        self.call("POST", "/events", {"data": {"type": "event", "attributes": event_attributes}})

    # --- reads, used by the live test to check what landed ------------------
    def find_profile(self, email: str) -> Optional[dict]:
        _, resp = self.call("GET", "/profiles?filter=" + urllib.request.quote(f'equals(email,"{email}")'))
        data = resp.get("data") or []
        return data[0] if data else None

    def metric_id(self, name: str) -> Optional[str]:
        _, resp = self.call("GET", "/metrics")
        while True:
            for m in resp.get("data") or []:
                if m["attributes"].get("name") == name:
                    return m["id"]
            nxt = (resp.get("links") or {}).get("next")
            if not nxt:
                return None
            _, resp = self.call("GET", nxt)

    def profile_events(self, profile_id: str, metric_id: str) -> list:
        f = urllib.request.quote(f'and(equals(profile_id,"{profile_id}"),equals(metric_id,"{metric_id}"))')
        _, resp = self.call("GET", f"/events?filter={f}")
        return resp.get("data") or []


# --- building the payloads ------------------------------------------------------

def _clean_email(order: Order) -> str:
    email = (order.customer_email or "").strip().lower()
    if not email:
        raise ValidationError("order has no customer email")
    if len(email) > 254 or not EMAIL_RE.match(email):
        raise ValidationError("customer email is not a valid address")
    return email


def _text(value, limit: int = MAX_TEXT) -> str:
    return str(value or "").strip()[:limit]


def _check_order(order: Order) -> None:
    if not isinstance(order.total, (int, float)) or not math.isfinite(order.total) or order.total < 0:
        raise ValidationError("order total must be a number of zero or more")
    if not _CURRENCY_RE.match(order.currency or ""):
        raise ValidationError("currency must be a 3 letter code")
    if not order.order_key:
        raise ValidationError("order has no key")
    for i in order.items:
        if i.quantity < 0 or not math.isfinite(i.unit_price) or i.unit_price < 0:
            raise ValidationError("a line item has a negative or invalid quantity or price")


def _split_name(name: str) -> Tuple[str, str]:
    first, _, last = _text(name).partition(" ")
    return first, last.strip()


def _items(order: Order) -> list:
    return [{"ProductName": _text(i.title), "Quantity": i.quantity, "ItemPrice": round(i.unit_price, 2),
             "RowTotal": round(i.quantity * i.unit_price, 2)} for i in order.items[:MAX_ITEMS]]


def epoch(iso: str) -> Optional[float]:
    try:
        return datetime.fromisoformat((iso or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def last_order_facts(order: Order) -> dict:
    return {"Last Order Date": order.created_at or None, "Last Order ID": _text(order.order_name),
            "Last Order Value": round(order.total, 2), "Last Order Currency": order.currency,
            "Last Order Source": order.source}


def profile_attributes(order: Order, order_count: int, newest: Optional[dict] = None) -> dict:
    """Email, name and the order facts a flow or segment filters on. `newest`
    is the customer's most recent order if it is not this one (a replay of an
    older order must not roll 'last order' back).
    Deliberately no 'subscriptions' key: consent is not touched."""
    email = _clean_email(order)
    _check_order(order)
    first, last = _split_name(order.customer_name)
    attrs = {"email": email,
             "properties": {"Order Count": int(order_count), **(newest or last_order_facts(order))}}
    if first:
        attrs["first_name"] = first
    if last:
        attrs["last_name"] = last
    attrs["properties"] = {k: v for k, v in attrs["properties"].items() if v is not None}
    return attrs


def _event(order: Order, metric: str, unique_id: str, extra: dict) -> dict:
    email = _clean_email(order)
    _check_order(order)
    items = _items(order)
    attrs = {
        "properties": {"OrderId": _text(order.order_name), "OrderKey": order.order_key,
                       "Source": order.source, "ItemNames": [i["ProductName"] for i in items],
                       "ItemCount": sum(i["Quantity"] for i in items), "Items": items,
                       "Currency": order.currency, "TestMode": bool(order.test), **extra},
        "value": round(order.total, 2),
        "value_currency": order.currency,
        "unique_id": unique_id[:255],
        "metric": {"data": {"type": "metric", "attributes": {"name": metric}}},
        "profile": {"data": {"type": "profile", "attributes": {"email": email}}},
    }
    if order.created_at:
        attrs["time"] = order.created_at
    return attrs


def placed_order_event(order: Order) -> dict:
    return _event(order, PLACED_ORDER, f"placed-order:{order.order_key}", {})


def started_checkout_event(order: Order, checkout_url: str = "", consent: str = "") -> dict:
    """consent is Stripe's consent.promotions value ('opt_in', 'opt_out' or empty).
    It is recorded as a plain property so a flow can filter on it; it is not
    turned into a Klaviyo subscription."""
    extra = {"CheckoutStatus": "expired", "StripePromotionsConsent": _text(consent) or "not collected"}
    if checkout_url:
        extra["CheckoutURL"] = _text(checkout_url, 2000)
    return _event(order, STARTED_CHECKOUT, f"started-checkout:{order.order_key}", extra)


class KlaviyoSync:
    """What the pipeline calls. Keeps the order count per customer in the event
    store (keyed by a hash of the email, never the email itself) because Stripe
    has no order count of its own; Shopify's orders_count wins when present."""

    def __init__(self, client: KlaviyoClient, store=None):
        self.client, self.store = client, store

    def order_history(self, order: Order) -> Tuple[int, dict]:
        facts = last_order_facts(order)
        count, newest = (self.store.record_customer_order(order.customer_email, order.order_key,
                                                          epoch(order.created_at), facts)
                         if self.store else (1, facts))
        if order.customer_order_count is not None:
            count = max(int(order.customer_order_count), 1)
        return count, newest or facts

    def placed_order(self, order: Order) -> dict:
        event = placed_order_event(order)       # validates before anything is recorded
        count, newest = self.order_history(order)
        attrs = profile_attributes(order, count, newest)
        profile_id = self.client.upsert_profile(attrs)
        self.client.create_event(event)
        log.info("Klaviyo: %s sent for %s (profile %s)%s", PLACED_ORDER, order.order_key,
                 profile_id or "-", " [dry run]" if self.client.dry_run else "")
        return {"profile_id": profile_id, "unique_id": event["unique_id"], "dry_run": self.client.dry_run}

    def started_checkout(self, order: Order, checkout_url: str = "", consent: str = "") -> dict:
        event = started_checkout_event(order, checkout_url, consent)
        self.client.create_event(event)   # the event creates the profile if Klaviyo has none
        log.info("Klaviyo: %s sent for %s%s", STARTED_CHECKOUT, order.order_key,
                 " [dry run]" if self.client.dry_run else "")
        return {"unique_id": event["unique_id"], "dry_run": self.client.dry_run}
