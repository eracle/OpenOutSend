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


# ── The verdict ───────────────────────────────────────────────────


def test_a_pass_with_nothing_failing_is_ok():
    assert PassResult().ok is True
    assert PassResult(failed=1).ok is False
