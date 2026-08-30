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
from cold_outreach.send_pass import PassResult, _what_is_holding, run_send_pass
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


def _waiting(address="lead@acme.com"):
    """A deal ready for its first email."""
    return DealFactory(lead=LeadFactory(email=address))


def _replied(box):
    """An EMAILED deal whose newest turn is theirs — the answer pool's whole trigger."""
    thread = Thread.objects.create(mailbox=box)
    deal = DealFactory(
        lead=LeadFactory(email="lead@acme.com"),
        state=DealState.EMAILED, mailbox=box, thread=thread,
    )
    maillog.outbound(box, thread=thread)
    maillog.inbound(box, thread=thread)
    return deal


def _free(box):
    """Hand the opener loop *box* once, then close the pool — one send's worth."""
    return patch.object(Mailbox.objects, "free_for_first_email", side_effect=[box, None])


# ── The order ─────────────────────────────────────────────────────


def test_the_mail_is_read_before_anything_is_written(steps):
    box = maillog.mailbox()
    _replied(box)
    _waiting("other@acme.com")

    with _free(box):
        run_send_pass()

    assert steps.order == ["read", "answer", "open"]


def test_the_counts_come_back_from_the_mail_pass(steps):
    steps.mail.side_effect = lambda: (4, 3, 2)

    result = run_send_pass()

    assert (result.mirrored, result.classified, result.projected) == (4, 3, 2)


# ── Answering ─────────────────────────────────────────────────────


def test_every_thread_that_replied_is_answered(steps):
    box = maillog.mailbox()
    _replied(box)
    _replied(box)

    result = run_send_pass()

    assert result.answered == 2
    assert steps.reply.call_count == 2


def test_the_state_a_step_returns_is_saved_with_it(steps):
    box = maillog.mailbox()
    deal = _replied(box)

    def complete(target):
        target.outcome = Outcome.NOT_INTERESTED
        return DealState.COMPLETED

    steps.reply.side_effect = complete
    run_send_pass()

    deal.refresh_from_db()
    assert (deal.state, deal.outcome) == (DealState.COMPLETED, Outcome.NOT_INTERESTED)


def test_a_step_that_stays_put_leaves_the_state_alone(steps):
    """`None` is a step deciding not to move — a lead suppressed while the agent wrote."""
    box = maillog.mailbox()
    deal = _replied(box)
    steps.reply.side_effect = lambda target: None

    run_send_pass()

    deal.refresh_from_db()
    assert deal.state == DealState.EMAILED


def test_a_failed_reply_costs_only_its_own_conversation(steps):
    box = maillog.mailbox()
    _replied(box)
    _replied(box)
    steps.reply.side_effect = [RuntimeError("smtp said no"), None]

    result = run_send_pass()

    assert (result.answered, result.failed) == (1, 1)
    assert result.ok is False


# ── Opening ───────────────────────────────────────────────────────


def test_one_conversation_is_opened_per_free_box(steps):
    """The guard is the bound: the box takes itself out of the pool as it sends."""
    box = maillog.mailbox()
    _waiting()
    _waiting("second@acme.com")

    with _free(box):
        result = run_send_pass()

    assert result.opened == 1


def test_a_free_box_with_nobody_waiting_opens_nothing(steps):
    box = maillog.mailbox()

    with _free(box):
        result = run_send_pass()

    assert result.opened == 0
    steps.send.assert_not_called()


def test_a_failed_opener_stops_the_openers_for_this_pass(steps):
    """A box that just refused a send is still 'free' — retrying would not terminate."""
    box = maillog.mailbox()
    _waiting()
    _waiting("second@acme.com")
    steps.send.side_effect = RuntimeError("smtp said no")

    with patch.object(Mailbox.objects, "free_for_first_email", return_value=box):
        result = run_send_pass()

    assert (result.opened, result.failed) == (0, 1)
    assert steps.send.call_count == 1


def test_no_free_box_means_no_openers(steps):
    _waiting()

    with patch.object(Mailbox.objects, "free_for_first_email", return_value=None):
        result = run_send_pass()

    assert result.opened == 0
    steps.send.assert_not_called()


# ── The line it prints ────────────────────────────────────────────


def test_the_gate_is_named_as_its_consequence_when_no_box_is_connected():
    _waiting()

    assert "no mailbox connected" in _what_is_holding()


def test_the_gate_is_named_when_the_day_is_spent():
    maillog.mailbox(daily_limit=0)

    assert "no send headroom left today" in _what_is_holding()


def test_the_gate_is_named_outside_sending_hours():
    maillog.mailbox()

    with patch("cold_outreach.core.sending_window.within_sending_window", return_value=False):
        assert "outside sending hours" in _what_is_holding()


def test_the_counts_are_said_even_when_nothing_is_holding():
    maillog.mailbox(daily_limit=5)
    _waiting()

    with patch("cold_outreach.core.sending_window.within_sending_window", return_value=True):
        line = _what_is_holding()

    assert "1 waiting to be emailed" in line
    assert "5 first email(s) left today" in line


# ── Follow-ups and openers share one budget ───────────────────────


def _silent(box, days_ago=10):
    """An EMAILED deal, written to once, that never answered."""
    from datetime import timedelta

    from django.utils import timezone

    thread = Thread.objects.create(mailbox=box)
    deal = DealFactory(
        lead=LeadFactory(email="silent@acme.com"),
        state=DealState.EMAILED, mailbox=box, thread=thread, email_subject="Hi",
    )
    maillog.outbound(box, thread=thread, message_id="old@infra.com",
                     sent_at=timezone.now() - timedelta(days=days_ago))
    return deal


def test_a_follow_up_and_an_opener_compete_for_the_same_slot(steps):
    """The whole fix for 102-follow-ups-to-1-opener: one budget, not two.

    A box with room for exactly one cold email, one lead waiting to be opened and one
    gone quiet. Only one message leaves — the follow-up, because it runs first — and
    the opener waits for the next pass rather than being sent out of a reserve.
    """
    box = maillog.mailbox(daily_limit=1)
    _silent(box)
    _waiting("new@acme.com")

    with patch("cold_outreach.core.sending_window.within_sending_window", return_value=True), \
            patch.object(Mailbox.objects, "free_for_first_email", side_effect=[box, None]):
        result = run_send_pass()

    assert steps.order == ["read", "follow_up", "open"]
    assert result.followed_up == 1


def test_nothing_is_chased_outside_the_sending_window(steps):
    """A follow-up is cold volume, so the clock speaks for it as it does for an opener."""
    box = maillog.mailbox(daily_limit=5)
    _silent(box)

    with patch("cold_outreach.core.sending_window.within_sending_window", return_value=False):
        result = run_send_pass()

    assert "follow_up" not in steps.order
    assert result.followed_up == 0


# ── The pass does not wait ────────────────────────────────────────


class TestAPassNeverSleeps:
    """The waiting belongs to `send_job.py`, which owns the whole pass while it waits.

    Keeping it out of here is what leaves the pass firable by a timer — and what lets a
    goal wait out the *window* and the day's headroom as well, which a sleep buried in
    one step of the pass could not do without holding the mail pass hostage for hours.
    """

    def test_a_closed_pool_ends_the_pass_rather_than_waiting_for_a_box(self, steps):
        """The box takes itself out of the pool on its way past, and that ends the pass
        — whether the wait is worth taking is the job's question, asked one level up."""
        box = maillog.mailbox()
        _waiting()
        _waiting("second@acme.com")

        with _free(box):
            result = run_send_pass()

        assert result.opened == 1

    def test_the_module_has_no_clock_to_sleep_on(self):
        """A regression guard with teeth: the loop that slept in here is gone, and so is
        the `time` import it slept with."""
        import cold_outreach.send_pass as module

        assert not hasattr(module, "time")


# ── The verdict ───────────────────────────────────────────────────


def test_a_pass_with_nothing_failing_is_ok():
    assert PassResult().ok is True
    assert PassResult(failed=1).ok is False
