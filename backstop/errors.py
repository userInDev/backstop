"""The error taxonomy is the whole project.

Everything that can happen when you ask a payment provider to move money
falls into exactly three buckets, and conflating any two of them is how
money moves twice.

    PayPalRefused   The provider ANSWERED and said no. Nothing moved.
                    Safe to retry, safe to tell the customer it failed.

    PayPalUnknown   The provider did not answer: a timeout, a 5xx, a dropped
                    connection, a process that died mid-call. The money may
                    or may not have moved. NOT safe to retry. Only the
                    reconciler, reading the provider's own list of refunds,
                    may settle it.

    Blocked         We refused before calling, because an unresolved attempt
                    already exists on this capture.

The failure mode this prevents: an agent treats a timeout as a failure,
retries, and refunds the customer twice.
"""


class BackstopError(Exception):
    """Base for everything this package raises."""


class PayPalRefused(BackstopError):
    """The provider answered with a refusal. Nothing moved."""

    def __init__(self, message: str, status: int | None = None, body: object = None):
        super().__init__(message)
        self.status = status
        self.body = body


class PayPalUnknown(BackstopError):
    """The provider did not answer. The outcome is genuinely unknown.

    Never downgrade this to PayPalRefused. A retry on an unknown is how you
    pay twice.
    """

    def __init__(self, message: str, attempt_id: str | None = None):
        super().__init__(message)
        self.attempt_id = attempt_id


class Blocked(BackstopError):
    """Refused before any call, because an unresolved attempt is open."""


class LineAlreadyRefunded(BackstopError):
    """A line item was already claimed by an attempt that is not `failed`.

    The provider's cap guards the TOTAL, not the item. Without this check you
    can refund the same cracked frame three times and never exceed the order
    total, so the provider will happily let you do it.
    """

    def __init__(self, line_ids):
        self.line_ids = sorted(line_ids)
        super().__init__(f"already claimed by an open or settled attempt: {', '.join(self.line_ids)}")


class ExceedsBalance(BackstopError):
    """Asked for more than the capture's live refundable balance."""
