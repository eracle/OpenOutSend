"""One bounded pass: read, answer, open — in that order, and then stop.

The steps themselves are tested next to their own code; what is tested here is the
pass that drives them — the order, the guards it obeys as bounds, what a failure costs,
and the line it prints when it did nothing.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cold_outreach.emails.models import Mailbox, Thread
from cold_outreach.leads.models import DealState, Outcome
from cold_outreach.send_pass import PassResult, _wait_for_spacing, _what_is_holding, run_send_pass
from cold_outreach.tests.emails import maillog
from cold_outreach.tests.factories import DealFactory, LeadFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def steps():
    """The four things a pass calls out to, stubbed, with the order they were called in."""
    order: list[str] = []
    with patch("cold_outreach.emails.mail_pass.run_mail_pass") as mail, \
            patch("cold_outreach.emails.steps.reply.answer_reply") as reply, \
            patch("cold_outreach.emails.steps.follow_up.send_follow_up") as chase, \
            patch("cold_outreach.emails.steps.send.send_first_email") as send:
        mail.side_effect = lambda: (order.append("read"), (0, 0, 0))[1]
        reply.side_effect = lambda deal: order.append("answer")
        chase.side_effect = lambda deal: order.append("follow_up")
        send.side_effect = lambda deal, mailbox, prompt_line: order.append("open")
        yield SimpleNamespace(mail=mail, reply=reply, chase=chase, send=send, order=order)


def _waiting(campaign, address="lead@acme.com"):
    """A deal ready for its first email."""
    return DealFactory(campaign=campaign, lead=LeadFactory(email=address))


def _replied(campaign, box):
    """An EMAILED deal whose newest turn is theirs — the answer pool's whole trigger."""
    thread = Thread.objects.create(mailbox=box)
    deal = DealFactory(
        campaign=campaign, lead=LeadFactory(email="lead@acme.com"),
        state=DealState.EMAILED, mailbox=box, thread=thread,
    )
    maillog.outbound(box, thread=thread)
    maillog.inbound(box, thread=thread)
    return deal


def _free(box):
    """Hand the opener loop *box* once, then close the pool — one send's worth."""
    return patch.object(Mailbox.objects, "free_for_first_email", side_effect=[box, None])


# ── The order ─────────────────────────────────────────────────────


def test_the_mail_is_read_before_anything_is_written(campaign, steps):
    box = maillog.mailbox()
    _replied(campaign, box)
    _waiting(campaign, "other@acme.com")

    with _free(box):
        run_send_pass(campaign)

    assert steps.order == ["read", "answer", "open"]


def test_the_counts_come_back_from_the_mail_pass(campaign, steps):
    steps.mail.side_effect = lambda: (4, 3, 2)

    result = run_send_pass(campaign)

    assert (result.mirrored, result.classified, result.projected) == (4, 3, 2)


# ── Answering ─────────────────────────────────────────────────────


def test_every_thread_that_replied_is_answered(campaign, steps):
    box = maillog.mailbox()
    _replied(campaign, box)
    _replied(campaign, box)

    result = run_send_pass(campaign)

    assert result.answered == 2
    assert steps.reply.call_count == 2


def test_the_state_a_step_returns_is_saved_with_it(campaign, steps):
    box = maillog.mailbox()
    deal = _replied(campaign, box)

    def complete(target):
        target.outcome = Outcome.NOT_INTERESTED
        return DealState.COMPLETED

    steps.reply.side_effect = complete
    run_send_pass(campaign)

    deal.refresh_from_db()
    assert (deal.state, deal.outcome) == (DealState.COMPLETED, Outcome.NOT_INTERESTED)


def test_a_step_that_stays_put_leaves_the_state_alone(campaign, steps):
    """`None` is a step deciding not to move — a lead suppressed while the agent wrote."""
    box = maillog.mailbox()
    deal = _replied(campaign, box)
    steps.reply.side_effect = lambda target: None

    run_send_pass(campaign)

    deal.refresh_from_db()
    assert deal.state == DealState.EMAILED


def test_a_failed_reply_costs_only_its_own_conversation(campaign, steps):
    box = maillog.mailbox()
    _replied(campaign, box)
    _replied(campaign, box)
    steps.reply.side_effect = [RuntimeError("smtp said no"), None]

    result = run_send_pass(campaign)

    assert (result.answered, result.failed) == (1, 1)
    assert result.ok is False


# ── Opening ───────────────────────────────────────────────────────


def test_one_conversation_is_opened_per_free_box(campaign, steps):
    """The guard is the bound: the box takes itself out of the pool as it sends."""
    box = maillog.mailbox()
    _waiting(campaign)
    _waiting(campaign, "second@acme.com")

    with _free(box):
        result = run_send_pass(campaign)

    assert result.opened == 1


def test_a_free_box_with_nobody_waiting_opens_nothing(campaign, steps):
    box = maillog.mailbox()

    with _free(box):
        result = run_send_pass(campaign)

    assert result.opened == 0
    steps.send.assert_not_called()


def test_a_failed_opener_stops_the_openers_for_this_pass(campaign, steps):
    """A box that just refused a send is still 'free' — retrying would not terminate."""
    box = maillog.mailbox()
    _waiting(campaign)
    _waiting(campaign, "second@acme.com")
    steps.send.side_effect = RuntimeError("smtp said no")

    with patch.object(Mailbox.objects, "free_for_first_email", return_value=box):
        result = run_send_pass(campaign)

    assert (result.opened, result.failed) == (0, 1)
    assert steps.send.call_count == 1


def test_no_free_box_means_no_openers(campaign, steps):
    _waiting(campaign)

    with patch.object(Mailbox.objects, "free_for_first_email", return_value=None):
        result = run_send_pass(campaign)

    assert result.opened == 0
    steps.send.assert_not_called()


# ── The line it prints ────────────────────────────────────────────


def test_the_gate_is_named_as_its_consequence_when_no_box_is_connected(campaign):
    _waiting(campaign)

    assert "no mailbox connected" in _what_is_holding(campaign)


def test_the_gate_is_named_when_the_day_is_spent(campaign):
    maillog.mailbox(daily_limit=0)

    assert "no send headroom left today" in _what_is_holding(campaign)


def test_the_gate_is_named_outside_sending_hours(campaign):
    maillog.mailbox()

    with patch("cold_outreach.core.sending_window.within_sending_window", return_value=False):
        assert "outside sending hours" in _what_is_holding(campaign)


def test_the_counts_are_said_even_when_nothing_is_holding(campaign):
    maillog.mailbox(daily_limit=5)
    _waiting(campaign)

    with patch("cold_outreach.core.sending_window.within_sending_window", return_value=True):
        line = _what_is_holding(campaign)

    assert "1 waiting to be emailed" in line
    assert "5 first email(s) left today" in line


# ── Follow-ups and openers share one budget ───────────────────────


def _silent(campaign, box, days_ago=10):
    """An EMAILED deal, written to once, that never answered."""
    from datetime import timedelta

    from django.utils import timezone

    thread = Thread.objects.create(mailbox=box)
    deal = DealFactory(
        campaign=campaign, lead=LeadFactory(email="silent@acme.com"),
        state=DealState.EMAILED, mailbox=box, thread=thread, email_subject="Hi",
    )
    maillog.outbound(box, thread=thread, message_id="old@infra.com",
                     sent_at=timezone.now() - timedelta(days=days_ago))
    return deal


def test_a_follow_up_and_an_opener_compete_for_the_same_slot(campaign, steps):
    """The whole fix for 102-follow-ups-to-1-opener: one budget, not two.

    A box with room for exactly one cold email, one lead waiting to be opened and one
    gone quiet. Only one message leaves — the follow-up, because it runs first — and
    the opener waits for the next pass rather than being sent out of a reserve.
    """
    box = maillog.mailbox(daily_limit=1)
    _silent(campaign, box)
    _waiting(campaign, "new@acme.com")

    with patch("cold_outreach.core.sending_window.within_sending_window", return_value=True), \
            patch.object(Mailbox.objects, "free_for_first_email", side_effect=[box, None]):
        result = run_send_pass(campaign)

    assert steps.order == ["read", "follow_up", "open"]
    assert result.followed_up == 1


def test_nothing_is_chased_outside_the_sending_window(campaign, steps):
    """A follow-up is cold volume, so the clock speaks for it as it does for an opener."""
    box = maillog.mailbox(daily_limit=5)
    _silent(campaign, box)

    with patch("cold_outreach.core.sending_window.within_sending_window", return_value=False):
        result = run_send_pass(campaign)

    assert "follow_up" not in steps.order
    assert result.followed_up == 0


# ── Goal-bounded opening ───────────────────────────────────────────


class TestGoalBoundedOpening:
    """`run_send_pass(campaign, goal=N)` — the finder's `job.py` lesson applied here:
    sleep through the short bound (spacing), stop at the long one (window/headroom)."""

    def test_with_no_goal_the_old_one_send_and_stop_shape_is_unchanged(self, campaign, steps):
        """Regression: `goal=None` must not sleep, loop, or change behavior at all."""
        box = maillog.mailbox()
        _waiting(campaign)
        _waiting(campaign, "second@acme.com")

        with _free(box), patch("cold_outreach.send_pass.time.sleep") as sleep:
            result = run_send_pass(campaign, goal=None)

        assert result.opened == 1
        sleep.assert_not_called()

    def test_a_goal_keeps_opening_across_the_spacing_clock(self, campaign, steps):
        """The point of `goal`: more than one send in one call, by sleeping through
        each box's own pacing gap rather than stopping at the first close."""
        box = maillog.mailbox(daily_limit=10)
        _waiting(campaign, "a@acme.com")
        _waiting(campaign, "b@acme.com")
        _waiting(campaign, "c@acme.com")

        # free, then "just spaced out" twice, then free again — `_wait_for_spacing`
        # is stubbed too, so the test does not depend on real send-spacing timestamps.
        with patch.object(Mailbox.objects, "free_for_first_email",
                          side_effect=[box, None, box, None, box]), \
                patch("cold_outreach.send_pass._wait_for_spacing", return_value=0.01), \
                patch("cold_outreach.send_pass.time.sleep") as sleep:
            result = run_send_pass(campaign, goal=3)

        assert result.opened == 3
        assert sleep.call_count == 2  # once for each of the two "not free yet" gaps

    def test_a_goal_stops_exactly_at_the_count_even_with_more_waiting(self, campaign, steps):
        box = maillog.mailbox(daily_limit=10)
        _waiting(campaign, "a@acme.com")
        _waiting(campaign, "b@acme.com")
        _waiting(campaign, "c@acme.com")

        with patch.object(Mailbox.objects, "free_for_first_email", return_value=box):
            result = run_send_pass(campaign, goal=2)

        assert result.opened == 2
        assert steps.send.call_count == 2

    def test_a_goal_stops_at_a_wall_it_cannot_sleep_through(self, campaign, steps):
        """Outside the window (or headroom spent), `_wait_for_spacing` is `None` — the
        pass reports how far it got rather than sleeping for hours."""
        _waiting(campaign)

        with patch.object(Mailbox.objects, "free_for_first_email", return_value=None), \
                patch("cold_outreach.send_pass._wait_for_spacing", return_value=None), \
                patch("cold_outreach.send_pass.time.sleep") as sleep:
            result = run_send_pass(campaign, goal=5)

        assert result.opened == 0
        sleep.assert_not_called()

    def test_a_failed_send_still_stops_the_goal_loop_for_this_pass(self, campaign, steps):
        box = maillog.mailbox()
        _waiting(campaign)
        _waiting(campaign, "second@acme.com")
        steps.send.side_effect = RuntimeError("smtp said no")

        with patch.object(Mailbox.objects, "free_for_first_email", return_value=box):
            result = run_send_pass(campaign, goal=5)

        assert (result.opened, result.failed) == (0, 1)
        assert steps.send.call_count == 1


class TestWaitForSpacing:
    """The helper deciding whether a closed pool is a few minutes from opening again,
    or a wall (window/headroom) that a goal-bounded pass must not sleep through."""

    def test_none_outside_the_sending_window(self):
        maillog.mailbox()

        with patch("cold_outreach.core.sending_window.within_sending_window",
                   return_value=False):
            assert _wait_for_spacing() is None

    def test_none_when_no_box_has_headroom_left_today(self):
        maillog.mailbox(daily_limit=0)

        with patch("cold_outreach.core.sending_window.within_sending_window",
                   return_value=True):
            assert _wait_for_spacing() is None

    def test_none_when_a_box_is_already_free(self):
        """Nothing to wait for — the caller should have used it, not slept."""
        maillog.mailbox(daily_limit=10)  # next_send_at is null: free now

        with patch("cold_outreach.core.sending_window.within_sending_window",
                   return_value=True):
            assert _wait_for_spacing() is None

    def test_a_positive_wait_when_a_box_is_only_waiting_on_spacing(self):
        from datetime import timedelta

        from django.utils import timezone

        box = maillog.mailbox(daily_limit=10)
        box.next_send_at = timezone.now() + timedelta(seconds=90)
        box.save()

        with patch("cold_outreach.core.sending_window.within_sending_window",
                   return_value=True):
            wait = _wait_for_spacing()

        assert wait is not None
        assert 88 <= wait <= 92  # ~90s plus the 1s safety margin


# ── The verdict ───────────────────────────────────────────────────


def test_a_pass_with_nothing_failing_is_ok():
    assert PassResult().ok is True
    assert PassResult(failed=1).ok is False
