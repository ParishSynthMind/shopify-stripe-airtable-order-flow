"""SQLite record of every webhook event, so a retried delivery never makes a
second Airtable row or a second email. Airtable's upsert on Order Key is the
second guard if two deliveries race each other."""
from __future__ import annotations

import sqlite3
import threading
import time
from typing import Optional

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
