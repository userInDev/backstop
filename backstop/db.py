"""SQLite, on purpose.

Judges have to run this themselves, and the rules say a hosted demo is not
required. Zero setup is worth more here than Postgres would be.

Money is stored in MINOR UNITS as integers (paise, cents). Never floats.
PayPal speaks decimal strings; conversion happens at that boundary only.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

# An attempt in one of these states may still be holding money. A new refund
# on the same capture is refused while any of them exist.
UNRESOLVED = ("pending", "unknown")

# These states have claimed their line items. `failed` has not: the provider
# answered no, nothing moved, so the lines are free again.
CLAIMING = ("pending", "unknown", "succeeded")

SCHEMA = """
CREATE TABLE IF NOT EXISTS captures (
    id            TEXT PRIMARY KEY,
    order_id      TEXT,
    currency      TEXT    NOT NULL,
    amount_minor  INTEGER NOT NULL,
    buyer_email   TEXT,
    created_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS lines (
    id            TEXT PRIMARY KEY,
    capture_id    TEXT    NOT NULL REFERENCES captures(id),
    sku           TEXT,
    description   TEXT    NOT NULL,
    amount_minor  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS refund_attempts (
    id                TEXT PRIMARY KEY,
    capture_id        TEXT    NOT NULL REFERENCES captures(id),
    amount_minor      INTEGER NOT NULL,
    currency          TEXT    NOT NULL,
    line_ids          TEXT    NOT NULL DEFAULT '[]',
    state             TEXT    NOT NULL,
    paypal_refund_id  TEXT,
    reason            TEXT,
    error             TEXT,
    created_at        TEXT    NOT NULL,
    settled_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_attempts_capture ON refund_attempts(capture_id, state);
CREATE INDEX IF NOT EXISTS idx_attempts_state   ON refund_attempts(state);
"""


def new_id() -> str:
    return uuid.uuid4().hex


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row(r: sqlite3.Row | None):
    """Rows as objects, so downstream reads `a.state` not `a["state"]`."""
    if r is None:
        return None
    d = dict(r)
    if "line_ids" in d and isinstance(d["line_ids"], str):
        d["line_ids"] = json.loads(d["line_ids"])
    return SimpleNamespace(**d)


def _rows(rs):
    return [_row(r) for r in rs]


class Db:
    def __init__(self, path: str | Path = "backstop.db"):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)

    def close(self):
        self.conn.close()

    # ---- captures -------------------------------------------------------

    def add_capture(self, *, id, order_id, currency, amount_minor, buyer_email=None):
        self.conn.execute(
            "INSERT OR REPLACE INTO captures (id, order_id, currency, amount_minor, buyer_email, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (id, order_id, currency, amount_minor, buyer_email, now()),
        )

    def capture(self, capture_id):
        return _row(self.conn.execute("SELECT * FROM captures WHERE id=?", (capture_id,)).fetchone())

    def captures(self):
        return _rows(self.conn.execute("SELECT * FROM captures ORDER BY created_at DESC").fetchall())

    def add_line(self, *, id, capture_id, description, amount_minor, sku=None):
        self.conn.execute(
            "INSERT OR REPLACE INTO lines (id, capture_id, sku, description, amount_minor) VALUES (?,?,?,?,?)",
            (id, capture_id, sku, description, amount_minor),
        )

    def lines(self, capture_id):
        return _rows(self.conn.execute("SELECT * FROM lines WHERE capture_id=? ORDER BY id", (capture_id,)).fetchall())

    # ---- attempts -------------------------------------------------------

    def open_attempt(self, *, capture_id, amount_minor, currency, line_ids, reason):
        """Rule: the attempt row is written BEFORE the provider call.

        If the process dies during the call, the evidence that we tried
        already exists on disk. A row written afterwards would be a row that
        never gets written in exactly the case that matters.
        """
        attempt_id = new_id()
        self.conn.execute(
            "INSERT INTO refund_attempts (id, capture_id, amount_minor, currency, line_ids, state, reason, created_at)"
            " VALUES (?,?,?,?,?,'pending',?,?)",
            (attempt_id, capture_id, amount_minor, currency, json.dumps(sorted(line_ids or [])), reason, now()),
        )
        return attempt_id

    def settle(self, attempt_id, state, *, paypal_refund_id=None, error=None):
        assert state in ("succeeded", "failed", "unknown"), state
        self.conn.execute(
            "UPDATE refund_attempts SET state=?, paypal_refund_id=COALESCE(?, paypal_refund_id),"
            " error=COALESCE(?, error), settled_at=? WHERE id=?",
            (state, paypal_refund_id, error, now() if state != "unknown" else None, attempt_id),
        )

    def attempt(self, attempt_id):
        return _row(self.conn.execute("SELECT * FROM refund_attempts WHERE id=?", (attempt_id,)).fetchone())

    def attempts(self, capture_id=None):
        if capture_id:
            q = "SELECT * FROM refund_attempts WHERE capture_id=? ORDER BY created_at DESC"
            return _rows(self.conn.execute(q, (capture_id,)).fetchall())
        return _rows(self.conn.execute("SELECT * FROM refund_attempts ORDER BY created_at DESC").fetchall())

    def unresolved(self, capture_id):
        """Attempts that may still be holding money on this capture."""
        marks = ",".join("?" * len(UNRESOLVED))
        q = f"SELECT * FROM refund_attempts WHERE capture_id=? AND state IN ({marks}) ORDER BY created_at"
        return _rows(self.conn.execute(q, (capture_id, *UNRESOLVED)).fetchall())

    def in_state(self, state):
        return _rows(self.conn.execute("SELECT * FROM refund_attempts WHERE state=? ORDER BY created_at", (state,)).fetchall())

    def claimed_lines(self, capture_id) -> set[str]:
        """Lines held by any attempt that is not `failed`."""
        marks = ",".join("?" * len(CLAIMING))
        q = f"SELECT line_ids FROM refund_attempts WHERE capture_id=? AND state IN ({marks})"
        claimed: set[str] = set()
        for (raw,) in self.conn.execute(q, (capture_id, *CLAIMING)).fetchall():
            claimed.update(json.loads(raw))
        return claimed

    def promote_stale_pending(self, older_than_seconds: int) -> list[str]:
        """A `pending` nobody finished is an abandoned claim, so it becomes `unknown`.

        A process that dies mid-call leaves `pending` behind forever. Treating
        that as "still in flight" would block the capture permanently; treating
        it as failed would invite a retry. It is unknown, which is the truth.
        """
        cutoff = datetime.now(timezone.utc).timestamp() - older_than_seconds
        promoted = []
        for a in self.in_state("pending"):
            created = datetime.fromisoformat(a.created_at).timestamp()
            if created <= cutoff:
                self.settle(a.id, "unknown", error="abandoned: pending with no outcome recorded")
                promoted.append(a.id)
        return promoted
