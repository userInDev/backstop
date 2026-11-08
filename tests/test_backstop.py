"""Every test here is a way money moves twice if the rule is removed."""

import pytest

from backstop import core, reconcile
from backstop.db import Db
from backstop.errors import Blocked, ExceedsBalance, LineAlreadyRefunded, PayPalRefused, PayPalUnknown
from fake_paypal import FakePayPal


@pytest.fixture
def db(tmp_path):
    d = Db(tmp_path / "t.db")
    d.add_capture(id="CAP1", order_id="ORD1", currency="USD", amount_minor=12000)
    d.add_line(id="L-frame", capture_id="CAP1", description="Walnut frame", amount_minor=4000)
    d.add_line(id="L-prints", capture_id="CAP1", description="Prints x24", amount_minor=8000)
    yield d
    d.close()


@pytest.fixture
def pp():
    return FakePayPal()


def test_happy_path(db, pp):
    r = core.refund(db, pp, capture_id="CAP1", line_ids=["L-frame"], reason="arrived cracked")
    assert r.state == "succeeded"
    assert r.amount_minor == 4000
    assert len(pp.refunds) == 1


def test_unknown_blocks_the_second_refund(db, pp):
    """The one that matters. PayPal recorded it; we never saw the response."""
    pp.next_failure = "lost"
    with pytest.raises(PayPalUnknown):
        core.refund(db, pp, capture_id="CAP1", line_ids=["L-frame"], reason="cracked")

    assert db.attempts("CAP1")[0].state == "unknown"
    assert len(pp.refunds) == 1  # the money DID move

    # The agent, knowing nothing, tries again. Without rule 1 this is a
    # second real refund and the customer is paid twice.
    with pytest.raises(Blocked):
        core.refund(db, pp, capture_id="CAP1", line_ids=["L-frame"], reason="cracked, retry")

    assert pp.refund_calls == 1
    assert len(pp.refunds) == 1


def test_reconciler_settles_unknown_that_really_happened(db, pp):
    pp.next_failure = "lost"
    with pytest.raises(PayPalUnknown):
        core.refund(db, pp, capture_id="CAP1", line_ids=["L-frame"], reason="cracked")

    report = reconcile.reconcile(db, pp)
    assert report.settled == [db.attempts("CAP1")[0].id]
    assert db.attempts("CAP1")[0].state == "succeeded"
    assert len(pp.refunds) == 1


def test_reconciler_clears_unknown_that_never_happened(db, pp):
    pp.next_failure = "unknown"  # never reached PayPal
    with pytest.raises(PayPalUnknown):
        core.refund(db, pp, capture_id="CAP1", line_ids=["L-frame"], reason="cracked")

    reconcile.reconcile(db, pp)
    attempt = db.attempts("CAP1")[0]
    assert attempt.state == "failed"

    # Now a retry is legitimate, and the lines are free again.
    r = core.refund(db, pp, capture_id="CAP1", line_ids=["L-frame"], reason="cracked, retry")
    assert r.state == "succeeded"


def test_reconciler_will_not_guess_when_paypal_cannot_answer(db, pp):
    pp.next_failure = "lost"
    with pytest.raises(PayPalUnknown):
        core.refund(db, pp, capture_id="CAP1", line_ids=["L-frame"], reason="cracked")

    pp.list_fails = True
    report = reconcile.reconcile(db, pp)
    assert report.settled == [] and report.cleared == []
    assert db.attempts("CAP1")[0].state == "unknown"


def test_refused_does_not_block_a_retry(db, pp):
    pp.next_failure = "refused"
    with pytest.raises(PayPalRefused):
        core.refund(db, pp, capture_id="CAP1", line_ids=["L-frame"], reason="cracked")
    assert db.attempts("CAP1")[0].state == "failed"

    r = core.refund(db, pp, capture_id="CAP1", line_ids=["L-frame"], reason="cracked, retry")
    assert r.state == "succeeded"


def test_same_line_cannot_be_refunded_twice(db, pp):
    core.refund(db, pp, capture_id="CAP1", line_ids=["L-frame"], reason="cracked")
    with pytest.raises(LineAlreadyRefunded) as e:
        core.refund(db, pp, capture_id="CAP1", line_ids=["L-frame"], reason="cracked again")
    assert e.value.line_ids == ["L-frame"]
    # The total cap would NOT have caught this: 4000 + 4000 < 12000.


def test_balance_is_read_live_so_dashboard_refunds_count(db, pp):
    """Somebody refunded 100.00 by hand in the PayPal dashboard.

    Our table knows nothing about it. A local-only balance check would
    happily refund the frame on top of it.
    """
    pp.refund_made_by_hand(10000)
    with pytest.raises(ExceedsBalance):
        core.refund(db, pp, capture_id="CAP1", amount_minor=4000, line_ids=["L-frame"], reason="cracked")
    assert pp.refund_calls == 0


def test_abandoned_pending_becomes_unknown_not_failed(db, pp):
    db.open_attempt(capture_id="CAP1", amount_minor=4000, currency="USD", line_ids=["L-frame"], reason="died mid call")
    report = reconcile.reconcile(db, pp, stale_pending_seconds=0)
    attempt = db.attempts("CAP1")[0]
    assert attempt.id in report.promoted
    # PayPal has no such refund, so reconcile then clears it in the same pass.
    assert attempt.state == "failed"


def test_unexpected_exception_is_unknown_not_failed(db, pp):
    def boom(**kwargs):
        raise ValueError("a bug we have not thought of")

    pp.refund_capture = boom
    with pytest.raises(PayPalUnknown):
        core.refund(db, pp, capture_id="CAP1", line_ids=["L-frame"], reason="cracked")
    assert db.attempts("CAP1")[0].state == "unknown"


def test_amount_must_match_the_lines(db, pp):
    with pytest.raises(Blocked):
        core.refund(db, pp, capture_id="CAP1", amount_minor=9999, line_ids=["L-frame"], reason="mismatch")
