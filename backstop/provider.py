"""Pick the provider once, here.

BACKSTOP_PROVIDER=fake (default) runs with no credentials and no network.
BACKSTOP_PROVIDER=paypal talks to the real sandbox.
"""

import os

_singleton = None


def get_provider(force=None):
    global _singleton
    if _singleton is not None and force is None:
        return _singleton
    kind = (force or os.getenv("BACKSTOP_PROVIDER") or "fake").lower()
    if kind == "paypal":
        from .paypal import PayPal
        _singleton = PayPal()
    else:
        from .fake import FakePayPal
        _singleton = FakePayPal()
        _singleton.chaos = os.getenv("BACKSTOP_CHAOS", "")
    return _singleton


def reset_provider():
    global _singleton
    _singleton = None
