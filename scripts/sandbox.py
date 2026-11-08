"""Tonight's job: find out what PayPal's sandbox actually does.

Run the steps in order. Each prints what it learned and what to run next.

    python scripts/sandbox.py order        # create a $120 order, print approve URL
    python scripts/sandbox.py capture ORD  # after approving in the browser
    python scripts/sandbox.py balance CAP  # live refundable balance
    python scripts/sandbox.py refund CAP 40.00
    python scripts/sandbox.py refunds ORD  # what PayPal believes exists

The questions this is here to answer, before any of the demo gets built:

  1. Does a partial refund work the way we assume?
  2. Does `custom_id` come back on the refund when we read the order again?
     The whole reconciler depends on it. If it does not survive the round
     trip, the handle has to move to `invoice_id` and we find that out now,
     not on the 12th.
  3. Can we capture without a browser, or does the demo need the approve
     step on camera?
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from backstop.db import Db, new_id
from backstop.errors import BackstopError
from backstop.money import from_provider, to_provider
from backstop.paypal import PayPal

CURRENCY = os.getenv("BACKSTOP_CURRENCY", "USD")


def _pp():
    pp = PayPal(chaos="")  # never chaos from this script
    if not pp.client_id or not pp.client_secret:
        sys.exit(
            "Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET first.\n"
            "  cp .env.example .env   then paste the sandbox REST app credentials from\n"
            "  https://developer.paypal.com/dashboard/applications/sandbox"
        )
    return pp


def cmd_order(_args):
    pp = _pp()
    order = pp.create_order(amount_minor=12000, currency=CURRENCY)
    approve = next((l["href"] for l in order.get("links", []) if l.get("rel") in ("approve", "payer-action")), None)
    print(json.dumps(order, indent=2))
    print("\norder id:", order["id"])
    print("approve in a browser as your sandbox BUYER, then run:")
    print(f"  python scripts/sandbox.py capture {order['id']}")
    if approve:
        print("\napprove URL:\n ", approve)


def cmd_capture(args):
    pp = _pp()
    order_id = args[0]
    body = pp.capture_order(order_id, request_id=new_id())
    print(json.dumps(body, indent=2))
    pu = body["purchase_units"][0]
    cap = pu["payments"]["captures"][0]
    amount_minor = from_provider(cap["amount"]["value"], cap["amount"]["currency_code"])

    db = Db(os.getenv("BACKSTOP_DB", "backstop.db"))
    db.add_capture(
        id=cap["id"], order_id=order_id,
        currency=cap["amount"]["currency_code"], amount_minor=amount_minor,
        buyer_email=(body.get("payer") or {}).get("email_address"),
    )
    # Two lines, so per-line refunds have something to bite on.
    db.add_line(id="L-frame", capture_id=cap["id"], description="Walnut frame 8x10", amount_minor=4000)
    db.add_line(id="L-prints", capture_id=cap["id"], description="Prints x24", amount_minor=8000)
    print("\ncapture id:", cap["id"], "stored locally with 2 lines")
    print(f"  python scripts/sandbox.py balance {cap['id']}")


def cmd_balance(args):
    pp, db = _pp(), Db(os.getenv("BACKSTOP_DB", "backstop.db"))
    capture_id = args[0]
    c = db.capture(capture_id)
    if not c:
        sys.exit(f"{capture_id} is not in the local db; run capture first")
    bal = pp.refundable_balance_minor(order_id=c.order_id, capture_id=capture_id, currency=c.currency)
    print(f"captured {to_provider(c.amount_minor, c.currency)} {c.currency}")
    print(f"refundable now {to_provider(bal, c.currency)} {c.currency}   (read live from PayPal)")


def cmd_refund(args):
    from backstop import core
    pp, db = _pp(), Db(os.getenv("BACKSTOP_DB", "backstop.db"))
    capture_id = args[0]
    c = db.capture(capture_id)
    if not c:
        sys.exit(f"{capture_id} is not in the local db; run capture first")
    line_ids = None
    amount_minor = None
    if len(args) > 1:
        if args[1].startswith("L-"):
            line_ids = args[1:]
        else:
            amount_minor = from_provider(args[1], c.currency)
    try:
        r = core.refund(db, pp, capture_id=capture_id, amount_minor=amount_minor,
                        line_ids=line_ids, reason="sandbox probe")
        print("refund", r.state, r.paypal_refund_id, to_provider(r.amount_minor, r.currency), r.currency)
    except BackstopError as e:
        print(f"{type(e).__name__}: {e}")


def cmd_refunds(args):
    pp = _pp()
    refunds = pp.live_refunds(args[0])
    print(json.dumps(refunds, indent=2))
    print("\ncustom_id survived the round trip:",
          all(r.get("custom_id") for r in refunds) if refunds else "no refunds yet")


COMMANDS = {"order": cmd_order, "capture": cmd_capture, "balance": cmd_balance,
            "refund": cmd_refund, "refunds": cmd_refunds}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.exit(__doc__)
    COMMANDS[sys.argv[1]](sys.argv[2:])
