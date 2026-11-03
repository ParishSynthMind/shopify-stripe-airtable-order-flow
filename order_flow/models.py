from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import List, Optional


@dataclass
class LineItem:
    title: str
    quantity: int
    unit_price: float


@dataclass
class Order:
    """One paid order, the same shape whichever platform it came from."""
    source: str                     # "Shopify" or "Stripe"
    order_key: str                  # stable id used for idempotency, e.g. "shopify:5551234"
    order_name: str                 # what a person calls it, e.g. "#1001"
    total: float
    currency: str
    customer_name: str = ""
    customer_email: str = ""
    items: List[LineItem] = field(default_factory=list)
    shipping_country: str = ""
    billing_country: str = ""
    card_country: str = ""
    customer_order_count: Optional[int] = None
    customer_note: str = ""
    payment_risk_level: str = ""    # Stripe Radar: normal / elevated / highest
    payment_risk_score: Optional[int] = None
    created_at: str = ""
    test: bool = False

    def items_text(self) -> str:
        return "\n".join(f"{i.quantity} x {i.title} @ {i.unit_price:.2f}" for i in self.items)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Review:
    note: str                       # short note for the team
    risk: str                       # Low / Medium / High
    flagged: bool                   # True = hold and check before fulfilling
    reasons: List[str] = field(default_factory=list)
    reviewer: str = "claude"        # "claude" or "rules" (fallback)
