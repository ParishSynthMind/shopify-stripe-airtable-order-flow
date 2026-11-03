"""Turn each platform's payload into one Order shape."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from .models import LineItem, Order

# Stripe amounts are in the smallest unit; these currencies have no minor unit.
ZERO_DECIMAL = {"bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga", "pyg",
                "rwf", "ugx", "vnd", "vuv", "xaf", "xof", "xpf"}


def _money(amount_minor: int, currency: str) -> float:
    return float(amount_minor) if currency.lower() in ZERO_DECIMAL else amount_minor / 100.0


def _iso(ts) -> str:
    if not ts:
        return ""
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    return str(ts)


# --- Shopify orders/paid ------------------------------------------------------

def from_shopify(payload: dict) -> Order:
    customer = payload.get("customer") or {}
    shipping = payload.get("shipping_address") or {}
    billing = payload.get("billing_address") or {}
    name = " ".join(p for p in (customer.get("first_name"), customer.get("last_name")) if p)
    if not name:
        name = shipping.get("name") or billing.get("name") or ""
    items = [
        LineItem(title=str(li.get("title") or li.get("name") or "item"),
                 quantity=int(li.get("quantity") or 0),
                 unit_price=float(li.get("price") or 0))
        for li in payload.get("line_items") or []
    ]
    count = customer.get("orders_count")
    return Order(
        source="Shopify",
        order_key=f"shopify:{payload['id']}",
        order_name=str(payload.get("name") or f"#{payload.get('order_number', payload['id'])}"),
        total=float(payload.get("total_price") or 0),
        currency=str(payload.get("currency") or "").upper(),
        customer_name=name,
        customer_email=str(payload.get("email") or customer.get("email") or ""),
        items=items,
        shipping_country=str(shipping.get("country_code") or ""),
        billing_country=str(billing.get("country_code") or ""),
        customer_order_count=int(count) if count is not None else None,
        customer_note=str(payload.get("note") or ""),
        created_at=_iso(payload.get("created_at")),
        test=bool(payload.get("test")),
    )


# --- Stripe ---------------------------------------------------------------------

def _metadata_items(metadata: dict, currency: str) -> list:
    """Optional `items` metadata: JSON list of {title, quantity, unit_price}."""
    raw = (metadata or {}).get("items")
    if not raw:
        return []
    try:
        return [LineItem(str(i["title"]), int(i.get("quantity", 1)), float(i.get("unit_price", 0)))
                for i in json.loads(raw)]
    except (ValueError, KeyError, TypeError):
        return [LineItem(title=str(raw)[:200], quantity=1, unit_price=0.0)]


def from_stripe_payment_intent(pi: dict, charge: Optional[dict] = None) -> Order:
    currency = str(pi.get("currency") or "")
    meta = pi.get("metadata") or {}
    shipping = pi.get("shipping") or {}
    charge = charge or {}
    billing = (charge.get("billing_details") or {})
    outcome = charge.get("outcome") or {}
    card = ((charge.get("payment_method_details") or {}).get("card") or {})
    amount = pi.get("amount_received") or pi.get("amount") or 0
    return Order(
        source="Stripe",
        order_key=f"stripe:{pi['id']}",
        order_name=str(meta.get("order_name") or pi["id"]),
        total=_money(amount, currency),
        currency=currency.upper(),
        customer_name=str(shipping.get("name") or billing.get("name") or meta.get("customer_name") or ""),
        customer_email=str(pi.get("receipt_email") or billing.get("email") or ""),
        items=_metadata_items(meta, currency),
        shipping_country=str((shipping.get("address") or {}).get("country") or ""),
        billing_country=str((billing.get("address") or {}).get("country") or ""),
        card_country=str(card.get("country") or ""),
        customer_note=str(meta.get("note") or ""),
        payment_risk_level=str(outcome.get("risk_level") or ""),
        payment_risk_score=outcome.get("risk_score"),
        created_at=_iso(pi.get("created")),
        test=not pi.get("livemode", False),
    )


def from_stripe_checkout_session(session: dict, charge: Optional[dict] = None) -> Order:
    """checkout.session.completed. Keyed on the PaymentIntent id so that the
    payment_intent.succeeded event for the same payment is treated as the same order."""
    currency = str(session.get("currency") or "")
    details = session.get("customer_details") or {}
    shipping = (session.get("collected_information") or {}).get("shipping_details") \
        or session.get("shipping_details") or {}
    meta = session.get("metadata") or {}
    charge = charge or {}
    outcome = charge.get("outcome") or {}
    card = ((charge.get("payment_method_details") or {}).get("card") or {})
    pi_id = session.get("payment_intent") or session["id"]
    return Order(
        source="Stripe",
        order_key=f"stripe:{pi_id}",
        order_name=str(meta.get("order_name") or session["id"]),
        total=_money(session.get("amount_total") or 0, currency),
        currency=currency.upper(),
        customer_name=str(details.get("name") or shipping.get("name") or ""),
        customer_email=str(details.get("email") or ""),
        items=_metadata_items(meta, currency),
        shipping_country=str((shipping.get("address") or {}).get("country") or ""),
        billing_country=str((details.get("address") or {}).get("country") or ""),
        card_country=str(card.get("country") or ""),
        customer_note=str(meta.get("note") or ""),
        payment_risk_level=str(outcome.get("risk_level") or ""),
        payment_risk_score=outcome.get("risk_score"),
        created_at=_iso(session.get("created")),
        test=not session.get("livemode", False),
    )
