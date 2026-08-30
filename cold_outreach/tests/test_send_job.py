"""`outsend send N`: keep passing until N conversations are open, waiting for the clocks.

The pass itself is `test_send_pass.py`'s subject. What is tested here is the run around
it — that it counts only openers, that it waits rather than spins, that every wait is
still spent reading the mail, and that it ends on something real instead of sleeping
forever.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from cold_outreach.core.conf import WAIT_SLICE_SECONDS
from cold_outreach.send_job import ALL, DRAINED, NO_MAILBOX, REFUSED, run_send_job
from cold_outreach.send_pass import PassResult
from cold_outreach.tests.factories import DealFactory, LeadFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def waiting():
    """Three leads waiting for a first email — a pool the goal cannot drain by accident."""
    for i in range(3):
        DealFactory(lead=LeadFactory(email=f"lead{i}@acme.com"))


@pytest.fixture
def slept():
    """Record every wait instead of taking it — a week of clock in a millisecond."""
    return []


def _run(goal, passes, slept, opening_in=timedelta(minutes=4)):
    """Run the job over a scripted sequence of passes.

    ``opening_in`` is what the mailbox pool answers when asked when it could next open a
    conversation — the only clock the job reads.
    """
    with patch("cold_outreach.send_job.run_send_pass", side_effect=passes) as pass_, \
            patch("cold_outreach.send_job._next_opening",
                  side_effect=lambda: timezone.now() + opening_in
                  if opening_in is not None else None):
        result = run_send_job(goal, sleep=slept.append)
    return result, pass_


# ── The goal ──────────────────────────────────────────────────────


def test_it_keeps_passing_until_the_goal_is_open(waiting, slept):
    """One box opens roughly one conversation per pass, so three is three passes."""
    result, pass_ = _run(3, [PassResult(opened=1)] * 3, slept)

    assert (result.opened, result.passes, result.reached) == (3, 3, True)
    assert pass_.call_count == 3


def test_only_openers_count_toward_the_goal(waiting, slept):
    """Answering a reply is not cold volume and is not what the operator asked for.

    A busy inbox must not satisfy `send 2` without opening anything.
    """
    result, _ = _run(2, [PassResult(answered=4, followed_up=3),
                         PassResult(opened=1),
                         PassResult(opened=1)], slept)

    assert result.opened == 2
    assert (result.totals.answered, result.totals.followed_up) == (4, 3)


def test_the_run_stops_the_moment_the_goal_is_met(waiting, slept):
    """A pass that opens two when one was left does not buy a third pass."""
    result, pass_ = _run(3, [PassResult(opened=1), PassResult(opened=2),
                             PassResult(opened=9)], slept)

    assert (result.opened, pass_.call_count) == (3, 2)
    assert len(slept) == 1  # one wait, between the two passes — none after the last


# ── The wait ──────────────────────────────────────────────────────


def test_it_waits_out_the_spacing_clock_after_a_send(waiting, slept):
    """Four minutes is the box's own `next_send_at`, not a number this module owns."""
    result, _ = _run(2, [PassResult(opened=1), PassResult(opened=1)], slept,
                     opening_in=timedelta(minutes=4))

    assert slept == [pytest.approx(240, abs=2)]
    assert result.reached


def test_a_long_wait_is_slept_in_slices_so_the_mail_is_still_read(waiting, slept):
    """The window shuts at 20:00 and `send 2` is allowed to wait for 08:00 — but a reply
    arriving at 21:00 must not sit unread until then, so each slice ends in a full pass."""
    passes = [PassResult()] * 4 + [PassResult(opened=2)]
    result, pass_ = _run(2, passes, slept, opening_in=timedelta(hours=12))

    assert slept == [WAIT_SLICE_SECONDS] * 4
    assert pass_.call_count == 5 and result.reached


def test_an_idle_pass_never_spins_even_when_a_box_is_free(waiting, slept):
    """The guards say a box is free and the pass opened nothing anyway. Asking again in
    the same instant is a spin — a hot loop on IMAP — so it still costs a slice."""
    _run(2, [PassResult(), PassResult(opened=2)], slept,
         opening_in=timedelta(seconds=-30))

    assert slept == [WAIT_SLICE_SECONDS]


# ── Endings that are not the goal ─────────────────────────────────


def test_a_drained_pool_ends_the_run_instead_of_waiting_for_leads(slept):
    """The one gate no clock resolves: leads arrive by a separate invocation."""
    result, _ = _run(5, [PassResult(opened=1)], slept)

    assert result.stopped_because == DRAINED
    assert "1 of 5" in result.detail and "nobody left to email" in result.detail
    assert slept == []


def test_no_mailbox_ends_the_run(waiting, slept):
    result, _ = _run(5, [PassResult()], slept, opening_in=None)

    assert result.stopped_because == NO_MAILBOX
    assert "outsend init" in result.detail


def test_two_refused_passes_end_the_run(waiting, slept):
    """A receiver saying no is not a clock. Waiting to hear it again is the daemon."""
    result, pass_ = _run(5, [PassResult(failed=1), PassResult(failed=1),
                             PassResult(opened=5)], slept)

    assert result.stopped_because == REFUSED
    assert pass_.call_count == 2


def test_one_failure_is_a_blip_and_the_next_pass_is_the_retry(waiting, slept):
    result, _ = _run(2, [PassResult(failed=1), PassResult(opened=2)], slept)

    assert result.reached and result.totals.failed == 1


def test_a_failure_alongside_an_opener_does_not_count_as_a_refusal(waiting, slept):
    """A box that opened one and failed another is working, not refusing."""
    result, _ = _run(3, [PassResult(opened=1, failed=1),
                         PassResult(opened=1, failed=1),
                         PassResult(opened=1)], slept)

    assert result.reached and result.opened == 3


def test_an_interrupt_hands_back_what_was_already_sent(waiting, slept):
    """Ctrl-C cannot unsend three emails, so it must not answer with a traceback."""
    with patch("cold_outreach.send_job.run_send_pass",
               side_effect=[PassResult(opened=3), KeyboardInterrupt()]), \
            patch("cold_outreach.send_job._next_opening",
                  side_effect=lambda: timezone.now()):
        result = run_send_job(9, sleep=slept.append)

    assert (result.opened, result.reached) == (3, False)
    assert "3 of 9" in result.detail


# ── What it says while it waits ───────────────────────────────────


def test_the_same_answer_is_not_reprinted_every_slice(waiting, slept, caplog):
    """A night of waiting is one line, not one every five minutes."""
    passes = [PassResult(holding="0 first email(s) left today")] * 4 + [PassResult(opened=1)]
    with caplog.at_level("INFO"):
        _run(1, passes, slept, opening_in=timedelta(hours=12))

    waits = [r.getMessage() for r in caplog.records if "next conversation can open" in r.getMessage()]
    assert len(waits) == 1
    assert "0 of 1" in waits[0] and "0 first email(s) left today" in waits[0]


# ── `send all`: the pool is the goal ──────────────────────────────


class TestTheGoalThatIsThePool:
    """`send all` is the same run with one verdict inverted: an empty pool is what it
    asked for, so the stop that fails a numbered goal succeeds here."""

    def test_it_keeps_going_until_nobody_is_left_to_email(self, waiting, slept):
        """Three waiting, one opened per pass, and it stops when the third is gone —
        the count comes from the pool rather than from anything typed."""
        from cold_outreach.leads.models import Deal

        def one_conversation(*args, **kwargs):
            Deal.objects.first().delete()
            return PassResult(opened=1)

        with patch("cold_outreach.send_job.run_send_pass", side_effect=one_conversation), \
                patch("cold_outreach.send_job._next_opening",
                      side_effect=lambda: timezone.now()):
            result = run_send_job(ALL, sleep=slept.append)

        assert (result.opened, result.passes) == (3, 3)
        assert result.drained_the_pool and result.reached and result.ok

    def test_a_drained_pool_fails_a_numbered_goal_and_satisfies_this_one(self, slept):
        """Same stop, same counts, opposite verdict — the whole difference `all` makes."""
        numbered, _ = _run(9, [PassResult(opened=1)], slept)
        every, _ = _run(ALL, [PassResult(opened=1)], slept)

        assert numbered.stopped_because == every.stopped_because == DRAINED
        assert numbered.reached is False and every.reached is True

    def test_it_says_how_far_it_got_without_inventing_a_total(self, slept):
        """There is no *of N* to report when the goal was never a number."""
        result, _ = _run(ALL, [PassResult(opened=2)], slept)

        assert result.progress == "2 opened"

    def test_the_endings_that_are_not_the_pool_still_fail(self, waiting, slept):
        """A missing box and a refusing receiver are failures however the goal was said."""
        no_box, _ = _run(ALL, [PassResult()], slept, opening_in=None)
        refused, _ = _run(ALL, [PassResult(failed=1)] * 2, slept)

        assert (no_box.stopped_because, no_box.reached) == (NO_MAILBOX, False)
        assert (refused.stopped_because, refused.reached) == (REFUSED, False)
