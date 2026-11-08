"""A PayPal that does what we tell it to, including losing responses.

This exists so the whole project runs with no credentials and no network.
Judges should be able to clone, install, and see the dangerous case in under a
minute. It is also what the tests run against.
"""

from .errors import PayPalRefused, PayPalUnknown
from .money import from_provider, to_provider


class FakePayPal:
    name = "fake"

    def __init__(self, *, captured_minor=12000, currency="USD"):
        self.currency = currency
        self.captured_minor = captured_minor
        self.refunds = []          # what "PayPal" believes exists
        self.next_failure = None   # "unknown" | "refused" | "lost"
        self.list_fails = False    # live_refunds itself cannot answer
        self.refund_calls = 0
        self.chaos = ""

    # -- reads ------------------------------------------------------------

    def live_refunds(self, order_id):
        if self.list_fails:
            raise PayPalUnknown("live_refunds: no answer")
        return list(self.refunds)

    def refundable_balance_minor(self, *, order_id, capture_id, currency):
        refunded = sum(from_provider(r["amount"]["value"], currency) for r in self.live_refunds(order_id))
        return self.captured_minor - refunded

    # -- the money move ---------------------------------------------------

    def refund_capture(self, *, capture_id, amount_minor, currency, attempt_id, note=None):
        self.refund_calls += 1
        mode = self.next_failure
        self.next_failure = None
        if mode is None and (self.chaos or "").lower() == "refund":
            mode = "lost"

        if mode == "refused":
            raise PayPalRefused("422 CAPTURE_FULLY_REFUNDED", 422)

        if mode == "lost":
            # PayPal DID record it. We just never saw the response. This is the
            # dangerous case and the one the demo turns on.
            self._record(attempt_id, amount_minor, currency)
            raise PayPalUnknown("refund: response discarded")

        if mode == "unknown":
            # Never reached PayPal at all. Also unknown to us.
            raise PayPalUnknown("refund: no answer")

        return self._record(attempt_id, amount_minor, currency)

    def _record(self, attempt_id, amount_minor, currency):
        entry = {
            "id": f"RF{len(self.refunds) + 1}",
            "custom_id": attempt_id,
            "amount": {"value": to_provider(amount_minor, currency), "currency_code": currency},
        }
        self.refunds.append(entry)
        return entry

    # -- what a human did in the dashboard --------------------------------

    def refund_made_by_hand(self, amount_minor):
        self.refunds.append({
            "id": f"RF-manual-{len(self.refunds) + 1}",
            "custom_id": None,
            "amount": {"value": to_provider(amount_minor, self.currency), "currency_code": self.currency},
        })
