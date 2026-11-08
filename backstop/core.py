"""The backstop.

This is the only path to a refund. The agent has no other tool, no direct
PayPal client, and no way to talk itself past any of these checks, because
they are not instructions in a prompt. They are code in front of the money.

The four rules, in the order they run:

  1. Refuse while anything is unresolved.  An attempt in `pending` or
     `unknown` on this capture may already have moved money. Until the
     reconciler settles it, nobody gets to try again.

  2. Refuse a line somebody already claimed.  The provider's cap guards the
     TOTAL, not the item. Refunding one cracked frame three times never
     exceeds the order total, so PayPal will cheerfully allow it.

  3. Read the balance LIVE from PayPal, never from our own table.  A refund
     issued by hand in the PayPal dashboard leaves no row here. The live read
     is also what makes a double submit safe.

  4. Write the attempt row BEFORE the call.  A row written after the call is
     a row that never gets written in exactly the case that matters: the
     process dying while PayPal is thinking.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import Blocked, ExceedsBalance, LineAlreadyRefunded, PayPalRefused, PayPalUnknown


@dataclass
class RefundResult:
    attempt_id: str
    state: str
    paypal_refund_id: str | None
    amount_minor: int
    currency: str


def refund(
    db,
    pp,
    *,
    capture_id: str,
    amount_minor: int | None = None,
    line_ids: list[str] | None = None,
    reason: str = "",
    allow_unlined: bool = False,
) -> RefundResult:
    capture = db.capture(capture_id)
    if capture is None:
        raise Blocked(f"unknown capture {capture_id}")

    line_ids = sorted(line_ids or [])
    if line_ids:
        known = {line.id for line in db.lines(capture_id)}
        missing = set(line_ids) - known
        if missing:
            raise Blocked(f"unknown lines on {capture_id}: {', '.join(sorted(missing))}")
        by_id = {line.id: line for line in db.lines(capture_id)}
        derived = sum(by_id[i].amount_minor for i in line_ids)
        if amount_minor is None:
            amount_minor = derived
        elif amount_minor != derived:
            raise Blocked(f"amount {amount_minor} does not match lines {derived}")

    if amount_minor is None or amount_minor <= 0:
        raise Blocked("refund amount must be positive")

    # ---- Rule 1: nothing unresolved may be outstanding ------------------
    open_attempts = db.unresolved(capture_id)
    if open_attempts:
        a = open_attempts[0]
        raise Blocked(
            f"attempt {a.id} on {capture_id} is {a.state}; refusing a second refund "
            f"until the reconciler settles it"
        )

    # ---- Rule 2: lines claimed by a non-failed attempt ------------------
    # A capture that HAS line items must be refunded BY line, or rule 2 is
    # trivially bypassed: a bare amount names nothing, so it claims nothing,
    # and the same cracked frame can be paid for twice while staying under the
    # total cap. A caller that genuinely means a free-form amount has to say so
    # out loud, and then it owns the consequence.
    if not line_ids and db.lines(capture_id) and not allow_unlined:
        raise Blocked(
            f"{capture_id} has line items; refund by line or pass allow_unlined=True. "
            f"A bare amount claims no lines and can pay for the same item twice."
        )

    if line_ids:
        overlap = set(line_ids) & db.claimed_lines(capture_id)
        if overlap:
            raise LineAlreadyRefunded(overlap)

    # ---- Rule 3: the balance comes from PayPal, live --------------------
    # A GET that times out costs us nothing: no money has moved and no attempt
    # row exists yet, so the caller may simply try again later.
    balance = pp.refundable_balance_minor(
        order_id=capture.order_id, capture_id=capture_id, currency=capture.currency
    )
    if amount_minor > balance:
        raise ExceedsBalance(
            f"asked {amount_minor}, PayPal says {balance} is refundable on {capture_id}"
        )

    # ---- Rule 4: evidence on disk before the call -----------------------
    attempt_id = db.open_attempt(
        capture_id=capture_id,
        amount_minor=amount_minor,
        currency=capture.currency,
        line_ids=line_ids,
        reason=reason,
    )

    try:
        body = pp.refund_capture(
            capture_id=capture_id,
            amount_minor=amount_minor,
            currency=capture.currency,
            attempt_id=attempt_id,
            note=reason,
        )
    except PayPalRefused as e:
        # PayPal ANSWERED. Nothing moved. The lines are free again.
        db.settle(attempt_id, "failed", error=str(e))
        raise
    except PayPalUnknown as e:
        e.attempt_id = attempt_id
        db.settle(attempt_id, "unknown", error=str(e))
        raise
    except Exception as e:
        # An unexpected exception is NOT a failure. We do not know whether the
        # request reached PayPal, so it is unknown, same as a timeout. This is
        # the clause that catches the bugs we have not thought of yet.
        db.settle(attempt_id, "unknown", error=f"unexpected: {e!r}")
        raise PayPalUnknown(f"unexpected during refund: {e!r}", attempt_id=attempt_id) from e

    refund_id = body.get("id")
    db.settle(attempt_id, "succeeded", paypal_refund_id=refund_id)
    return RefundResult(attempt_id, "succeeded", refund_id, amount_minor, capture.currency)
