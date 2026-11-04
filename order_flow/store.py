"""SQLite record of every webhook event, so a retried delivery never makes a
second Airtable row or a second email. Airtable's upsert on Order Key is the
second guard if two deliveries race each other."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from typing import Optional, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id   TEXT PRIMARY KEY,
    order_key  TEXT NOT NULL,
    source     TEXT NOT NULL,
    status     TEXT NOT NULL,          -- queued / done / failed / duplicate
    attempts   INTEGER NOT NULL DEFAULT 0,
    record_id  TEXT,
    error      TEXT,
    body       BLOB,                   -- kept only while failed, for replay
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_order ON events(order_key);

-- Klaviyo sync, kept apart from the order itself: a Klaviyo outage must never
-- hold up the Airtable row or the team email.
CREATE TABLE IF NOT EXISTS klaviyo_sync (
    event_id   TEXT PRIMARY KEY,
    order_key  TEXT NOT NULL,
    kind       TEXT NOT NULL,          -- placed / checkout
    status     TEXT NOT NULL,          -- done / failed / skipped
    attempts   INTEGER NOT NULL DEFAULT 0,
    detail     TEXT,
    body       BLOB,                   -- kept only while failed, for replay
    updated_at REAL NOT NULL
);
-- Orders per customer, for Klaviyo's order count and "last order". Only a
-- hash of the email is kept, never the address.
CREATE TABLE IF NOT EXISTS customer_orders (
    email_hash TEXT NOT NULL,
    order_key  TEXT NOT NULL,
    ordered_at REAL,                   -- epoch seconds, so time zones compare correctly
    facts      TEXT,                   -- JSON: order id, value, currency, source, date
    PRIMARY KEY (email_hash, order_key)
);
"""


class EventStore:
    def __init__(self, path: str = ":memory:"):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(SCHEMA)

    def claim(self, event_id: str, order_key: str, source: str) -> str:
        """Returns 'new' if this event should be processed, otherwise why not:
        'duplicate_event' (same delivery seen) or 'duplicate_order' (another
        event for the same order already finished)."""
        with self._lock:
            row = self._db.execute("SELECT status FROM events WHERE event_id=?", (event_id,)).fetchone()
            if row and row[0] != "failed":
                return "duplicate_event"
            done = self._db.execute(
                "SELECT 1 FROM events WHERE order_key=? AND status='done' AND event_id<>?",
                (order_key, event_id)).fetchone()
            if done:
                self._db.execute(
                    "INSERT OR REPLACE INTO events(event_id, order_key, source, status, updated_at) "
                    "VALUES (?,?,?,?,?)", (event_id, order_key, source, "duplicate", time.time()))
                self._db.commit()
                return "duplicate_order"
            self._db.execute(
                "INSERT OR REPLACE INTO events(event_id, order_key, source, status, attempts, updated_at) "
                "VALUES (?,?,?,'queued',COALESCE((SELECT attempts FROM events WHERE event_id=?),0),?)",
                (event_id, order_key, source, event_id, time.time()))
            self._db.commit()
            return "new"

    def mark_done(self, event_id: str, record_id: str) -> None:
        with self._lock:
            self._db.execute("UPDATE events SET status='done', record_id=?, error=NULL, body=NULL, "
                             "updated_at=? WHERE event_id=?", (record_id, time.time(), event_id))
            self._db.commit()

    def mark_failed(self, event_id: str, error: str, body: Optional[bytes] = None) -> None:
        with self._lock:
            self._db.execute("UPDATE events SET status='failed', attempts=attempts+1, error=?, body=?, "
                             "updated_at=? WHERE event_id=?", (error[:500], body, time.time(), event_id))
            self._db.commit()

    def status(self, event_id: str) -> Optional[dict]:
        row = self._db.execute("SELECT event_id, order_key, source, status, attempts, record_id, error "
                               "FROM events WHERE event_id=?", (event_id,)).fetchone()
        if not row:
            return None
        keys = ["event_id", "order_key", "source", "status", "attempts", "record_id", "error"]
        return dict(zip(keys, row))

    def failed(self) -> list:
        rows = self._db.execute("SELECT event_id, source, body FROM events WHERE status='failed'").fetchall()
        return [{"event_id": r[0], "source": r[1], "body": r[2]} for r in rows]

    # --- Klaviyo -------------------------------------------------------------
    def klaviyo_mark(self, event_id: str, order_key: str, kind: str, status: str,
                     detail: str = "", body: Optional[bytes] = None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO klaviyo_sync(event_id, order_key, kind, status, attempts, detail, body, updated_at) "
                "VALUES (?,?,?,?,1,?,?,?) ON CONFLICT(event_id) DO UPDATE SET status=excluded.status, "
                "attempts=attempts+1, detail=excluded.detail, body=excluded.body, updated_at=excluded.updated_at",
                (event_id, order_key, kind, status, detail[:500], body if status == "failed" else None, time.time()))
            self._db.commit()

    def klaviyo_status(self, event_id: str) -> Optional[dict]:
        row = self._db.execute("SELECT event_id, order_key, kind, status, attempts, detail FROM klaviyo_sync "
                               "WHERE event_id=?", (event_id,)).fetchone()
        keys = ["event_id", "order_key", "kind", "status", "attempts", "detail"]
        return dict(zip(keys, row)) if row else None

    def klaviyo_failed(self) -> list:
        rows = self._db.execute("SELECT k.event_id, k.kind, e.source, k.body FROM klaviyo_sync k "
                                "JOIN events e ON e.event_id = k.event_id WHERE k.status='failed'").fetchall()
        return [{"event_id": r[0], "kind": r[1], "source": r[2], "body": r[3]} for r in rows]

    def record_customer_order(self, email: str, order_key: str, ordered_at: Optional[float] = None,
                              facts: Optional[dict] = None) -> Tuple[int, Optional[dict]]:
        """Records this order against the customer (idempotent) and returns
        (how many distinct orders they have had here, facts of their newest one).
        A replay of an old order therefore never becomes the 'last order'."""
        h = hashlib.sha256((email or "").strip().lower().encode()).hexdigest()
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO customer_orders(email_hash, order_key, ordered_at, facts) "
                             "VALUES (?,?,?,?)", (h, order_key, ordered_at, json.dumps(facts or {})))
            self._db.commit()
            count = self._db.execute("SELECT COUNT(*) FROM customer_orders WHERE email_hash=?", (h,)).fetchone()[0]
            newest = self._db.execute("SELECT facts FROM customer_orders WHERE email_hash=? "
                                      "ORDER BY COALESCE(ordered_at, 0) DESC, rowid DESC LIMIT 1", (h,)).fetchone()
            return count, (json.loads(newest[0]) if newest and newest[0] else None)
