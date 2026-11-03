from __future__ import annotations

from .models import Order, Review


def order_email(order: Order, review: Review, record_id: str, prefix: str = "") -> tuple:
    flag = "FLAGGED, check before shipping" if review.flagged else f"risk {review.risk}"
    subject = f"{prefix}New paid order {order.order_name}: {order.total:.2f} {order.currency}, {flag}"
    lines = [
        f"New paid order from {order.source}{' (test mode)' if order.test else ''}.",
        "",
        f"Order:     {order.order_name}",
        f"Total:     {order.total:.2f} {order.currency}",
        f"Customer:  {order.customer_name or '(no name)'}",
        f"Ship to:   {order.shipping_country or '(not given)'}",
        "",
        "Items:",
        order.items_text() or "(none listed)",
        "",
        f"Note ({review.reviewer}): {review.note}",
        f"Risk: {review.risk}{'  ** FLAGGED **' if review.flagged else ''}",
    ]
    if review.reasons:
        lines += ["Why:"] + [f"  - {r}" for r in review.reasons]
    lines += ["", f"Airtable record: {record_id}"]
    return subject, "\n".join(lines)
