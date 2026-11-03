"""Airtable: one row per paid order, written with an upsert on Order Key so a
retry updates the same row instead of adding a second one."""
from __future__ import annotations

import urllib.parse
from typing import Callable, Optional

from .http import request
from .models import Order, Review

API = "https://api.airtable.com/v0"

# Field definitions used by scripts/setup_airtable.py to create the table.
TABLE_FIELDS = [
    {"name": "Order Key", "type": "singleLineText"},
    {"name": "Order", "type": "singleLineText"},
    {"name": "Source", "type": "singleSelect", "options": {"choices": [{"name": "Shopify"}, {"name": "Stripe"}]}},
    {"name": "Total", "type": "number", "options": {"precision": 2}},
    {"name": "Currency", "type": "singleLineText"},
    {"name": "Customer", "type": "singleLineText"},
    {"name": "Email", "type": "email"},
    {"name": "Items", "type": "multilineText"},
    {"name": "Ship To", "type": "singleLineText"},
    {"name": "Risk", "type": "singleSelect",
     "options": {"choices": [{"name": "Low"}, {"name": "Medium"}, {"name": "High"}]}},
    {"name": "Flagged", "type": "checkbox", "options": {"icon": "flag", "color": "redBright"}},
    {"name": "AI Note", "type": "multilineText"},
    {"name": "Risk Reasons", "type": "multilineText"},
    {"name": "Reviewed By", "type": "singleLineText"},
    {"name": "Event ID", "type": "singleLineText"},
    {"name": "Test", "type": "checkbox", "options": {"icon": "check", "color": "grayBright"}},
    {"name": "Received At", "type": "dateTime",
     "options": {"timeZone": "utc", "dateFormat": {"name": "iso"}, "timeFormat": {"name": "24hour"}}},
]


def order_fields(order: Order, review: Review, event_id: str, received_at: str) -> dict:
    return {
        "Order Key": order.order_key,
        "Order": order.order_name,
        "Source": order.source,
        "Total": round(order.total, 2),
        "Currency": order.currency,
        "Customer": order.customer_name,
        "Email": order.customer_email or None,
        "Items": order.items_text(),
        "Ship To": order.shipping_country,
        "Risk": review.risk,
        "Flagged": review.flagged,
        "AI Note": review.note,
        "Risk Reasons": "\n".join(review.reasons),
        "Reviewed By": review.reviewer,
        "Event ID": event_id,
        "Test": order.test,
        "Received At": received_at,
    }


class AirtableClient:
    def __init__(self, token: str, base_id: str, table: str = "Orders",
                 send: Optional[Callable] = None):
        self.token, self.base_id, self.table = token, base_id, table
        self._send = send or request

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def _table_url(self) -> str:
        return f"{API}/{self.base_id}/{urllib.parse.quote(self.table)}"

    def upsert_order(self, fields: dict) -> dict:
        """Returns {'record_id': ..., 'created': bool}."""
        clean = {k: v for k, v in fields.items() if v is not None}
        body = {
            "performUpsert": {"fieldsToMergeOn": ["Order Key"]},
            "typecast": True,
            "records": [{"fields": clean}],
        }
        resp = self._send("Airtable", "PATCH", self._table_url(), self._headers(), json_body=body)
        record = resp["records"][0]
        return {"record_id": record["id"], "created": record["id"] in resp.get("createdRecords", [])}

    def list_orders(self, max_records: int = 50) -> list:
        url = self._table_url() + f"?maxRecords={max_records}"
        return self._send("Airtable", "GET", url, self._headers()).get("records", [])

    # --- schema helpers, used once at setup ---
    def list_bases(self) -> list:
        return self._send("Airtable", "GET", f"{API}/meta/bases", self._headers()).get("bases", [])

    def list_tables(self) -> list:
        return self._send("Airtable", "GET", f"{API}/meta/bases/{self.base_id}/tables",
                          self._headers()).get("tables", [])

    def create_table(self) -> dict:
        body = {"name": self.table, "description": "Paid orders, one row each, written by the order flow.",
                "fields": TABLE_FIELDS}
        return self._send("Airtable", "POST", f"{API}/meta/bases/{self.base_id}/tables",
                          self._headers(), json_body=body)
