"""Demo data, so the console has something to show with no PayPal at all."""

import os

from .db import Db

DEMO_CAPTURE = "CAP-DEMO-1"
DEMO_ORDER = "ORD-DEMO-1"


def seed(db: Db | None = None, *, reset=True) -> Db:
    db = db or Db(os.getenv("BACKSTOP_DB", "backstop.db"))
    if reset:
        db.conn.execute("DELETE FROM refund_attempts")
        db.conn.execute("DELETE FROM memory")
        db.conn.execute("DELETE FROM lines")
        db.conn.execute("DELETE FROM captures")
    db.seed_policy()
    db.add_capture(id=DEMO_CAPTURE, order_id=DEMO_ORDER, currency="USD",
                   amount_minor=12000, buyer_email="buyer@example.com")
    db.add_line(id="L-frame", capture_id=DEMO_CAPTURE, sku="FRM-8X10",
                description="Walnut frame 8x10", amount_minor=4000)
    db.add_line(id="L-prints", capture_id=DEMO_CAPTURE, sku="PRT-24",
                description="Prints x24", amount_minor=8000)
    return db


if __name__ == "__main__":
    d = seed()
    print(f"seeded {DEMO_CAPTURE}: {[l.id for l in d.lines(DEMO_CAPTURE)]}")
