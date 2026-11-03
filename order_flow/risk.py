"""Plain rule checks that run before the AI step.

They give Claude concrete facts to reason about, and they are the fallback
review if the AI call fails, so an order is never lost or waved through
unchecked just because the model was unavailable.
"""
from __future__ import annotations

from typing import List

from .models import Order, Review


def signals(order: Order, high_value: float = 500.0, bulk_qty: int = 10) -> List[str]:
    out = []
    if order.total >= high_value:
        out.append(f"High value order: {order.total:.2f} {order.currency} (threshold {high_value:.2f})")
    if order.shipping_country and order.billing_country and order.shipping_country != order.billing_country:
        out.append(f"Shipping country {order.shipping_country} differs from billing country {order.billing_country}")
    if order.card_country and order.billing_country and order.card_country != order.billing_country:
        out.append(f"Card issued in {order.card_country}, billing country {order.billing_country}")
    if order.payment_risk_level in ("elevated", "highest"):
        score = f", score {order.payment_risk_score}" if order.payment_risk_score is not None else ""
        out.append(f"Stripe Radar risk level: {order.payment_risk_level}{score}")
    for item in order.items:
        if item.quantity >= bulk_qty:
            out.append(f"Bulk quantity: {item.quantity} x {item.title}")
    if not order.customer_email:
        out.append("No customer email on the order")
    if order.customer_order_count is not None and order.customer_order_count <= 1 and order.total >= high_value:
        out.append("First order from this customer and it is high value")
    return out


def rules_review(order: Order, found: List[str]) -> Review:
    """Used when the AI step is unavailable. Conservative on purpose."""
    hard = order.payment_risk_level == "highest"
    risk = "High" if hard or len(found) >= 2 else ("Medium" if found else "Low")
    note = (f"Paid order {order.order_name}, {len(order.items)} line(s), "
            f"{order.total:.2f} {order.currency}. Rule check only, no AI note.")
    return Review(note=note, risk=risk, flagged=risk != "Low", reasons=found, reviewer="rules")
