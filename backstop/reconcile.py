"""The only thing allowed to settle an `unknown`.

An unknown attempt is a question: did PayPal record my refund or not? Only
PayPal can answer it, and only its own list of refunds counts as the answer.

Two rules, and they are the same shape as the ones a subscription reconciler
needs:

  * Only an ANSWER may settle. If the list read itself fails, the attempt
    stays unknown. A timeout is not evidence of absence.

  * An abandoned `pending` becomes `unknown`, not `failed`. A process that
    died mid-call leaves `pending` behind; calling that a failure would
    invite the retry we exist to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import PayPalUnknown


@dataclass
class ReconcileReport:
    promoted: list[str] = field(default_factory=list)   # pending -> unknown
    settled: list[str] = field(default_factory=list)    # unknown -> succeeded
    cleared: list[str] = field(default_factory=list)    # unknown -> failed
    still_unknown: list[str] = field(default_factory=list)

    def as_dict(self):
        return {
            "promoted": self.promoted,
            "settled": self.settled,
            "cleared": self.cleared,
            "still_unknown": self.still_unknown,
        }


def reconcile(db, pp, *, stale_pending_seconds: int = 30) -> ReconcileReport:
    report = ReconcileReport()
    report.promoted = db.promote_stale_pending(stale_pending_seconds)

    for attempt in db.in_state("unknown"):
        capture = db.capture(attempt.capture_id)
        if capture is None:
            report.still_unknown.append(attempt.id)
            continue
        try:
            refunds = pp.live_refunds(capture.order_id)
        except PayPalUnknown:
            # No answer. Absence of evidence is not evidence of absence.
            report.still_unknown.append(attempt.id)
            continue

        match = next((r for r in refunds if r.get("custom_id") == attempt.id), None)
        if match:
            db.settle(attempt.id, "succeeded", paypal_refund_id=match.get("id"))
            report.settled.append(attempt.id)
        else:
            # PayPal answered with a complete list and our attempt is not in
            # it. That is an answer: the money did not move.
            db.settle(attempt.id, "failed", error="reconciled: no refund with this custom_id at PayPal")
            report.cleared.append(attempt.id)

    return report
