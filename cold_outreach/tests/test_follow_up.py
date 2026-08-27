"""Writing again to somebody who never answered.

Chasing was deleted once, for a reason that was really about *priority*: follow-ups
outranked openers out of a separate budget, and a live install measured 102 follow-ups
against 1 first email in a week. These tests hold the shape that makes that
unreachable — one budget, a cap on touches, and a gap measured backwards from the mail
log rather than scheduled on a row.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from cold_outreach.core.conf import FOLLOW_UP_GAPS_BUSINESS_DAYS, MAX_COLD_TOUCHES
from cold_outreach.emails.models import Thread
from cold_outreach.leads.models import DealState
from cold_outreach.leads.pools import awaiting_follow_up, exhausted_touches
from cold_outreach.tests.emails import maillog
from cold_outreach.tests.factories import DealFactory, LeadFactory

pytestmark = pytest.mark.django_db


def _monday(hour=10):
    """A fixed Monday, so a test never straddles a weekend by accident."""
    return timezone.now().replace(
        year=2026, month=3, day=16, hour=hour, minute=0, second=0, microsecond=0)


def _pursued(campaign, *, touches=1, days_ago=10, replied=False, box=None):
    """A deal with *touches* outbound turns, the last one *days_ago* business days back."""
    box = box or maillog.mailbox()
    thread = Thread.objects.create(mailbox=box)
    deal = DealFactory(
        campaign=campaign,
        lead=LeadFactory(email="lead@acme.com"),
        state=DealState.EMAILED,
        thread=thread,
        mailbox=box,
        email_subject="Hi there",
    )
    when = _monday() - timedelta(days=days_ago)
    for index in range(touches):
        maillog.outbound(box, thread=thread, message_id=f"out{index}@infra.com",
                         sent_at=when - timedelta(days=touches - index - 1))
    if replied:
        maillog.inbound(box, thread=thread, body="Sure, tell me more",
                        sent_at=when + timedelta(hours=1))
    return deal


class TestWhoIsDue:
    def test_a_silent_lead_past_the_gap_is_due(self, campaign):
        _pursued(campaign, touches=1, days_ago=10)

        assert len(awaiting_follow_up(campaign, now=_monday())) == 1

    def test_a_lead_inside_the_gap_is_not(self, campaign):
        _pursued(campaign, touches=1, days_ago=1)

        assert awaiting_follow_up(campaign, now=_monday()) == []

    def test_a_lead_who_replied_is_never_chased(self, campaign):
        """Even after we answered them — a reply moves the thread out of this pool for good."""
        _pursued(campaign, touches=1, days_ago=10, replied=True)

        assert awaiting_follow_up(campaign, now=_monday()) == []

    def test_a_lead_out_of_touches_is_not_due_but_is_exhausted(self, campaign):
        _pursued(campaign, touches=MAX_COLD_TOUCHES, days_ago=30)

        assert awaiting_follow_up(campaign, now=_monday()) == []
        assert exhausted_touches(campaign).count() == 1

    def test_a_lead_still_within_the_cap_is_not_exhausted(self, campaign):
        _pursued(campaign, touches=MAX_COLD_TOUCHES - 1, days_ago=30)

        assert exhausted_touches(campaign).count() == 0

    def test_the_gap_widens_with_each_touch(self, campaign):
        """The second gap is longer than the first, so touch three waits longer."""
        first, second = FOLLOW_UP_GAPS_BUSINESS_DAYS[:2]
        assert second > first

        # Waited long enough for the first gap, not for the second.
        _pursued(campaign, touches=2, days_ago=first + 1)

        assert awaiting_follow_up(campaign, now=_monday()) == []

    def test_a_weekend_does_not_count_toward_the_gap(self, campaign):
        """Friday → Monday is one working day, not three."""
        box = maillog.mailbox()
        thread = Thread.objects.create(mailbox=box)
        DealFactory(campaign=campaign, lead=LeadFactory(email="l@acme.com"),
                    state=DealState.EMAILED, thread=thread, mailbox=box)
        friday = _monday() - timedelta(days=3)
        maillog.outbound(box, thread=thread, message_id="out@infra.com", sent_at=friday)

        assert awaiting_follow_up(campaign, now=_monday()) == []


class TestGivingUp:
    def test_the_deal_completes_as_unresponsive(self, campaign):
        from cold_outreach.emails.steps.follow_up import give_up

        deal = _pursued(campaign, touches=MAX_COLD_TOUCHES, days_ago=30)

        assert give_up(deal) == DealState.COMPLETED
        assert deal.outcome == "unresponsive"

    def test_silence_never_suppresses_the_address(self, campaign):
        """Not answering is not asking to stop — a later campaign may still suit them."""
        from cold_outreach.emails.steps.follow_up import give_up
        from cold_outreach.leads.suppression import is_suppressed

        deal = _pursued(campaign, touches=MAX_COLD_TOUCHES, days_ago=30)
        give_up(deal)

        assert not is_suppressed(deal.lead.email)


class TestTheFollowUpItself:
    def _send(self, deal, message="One more thought.", action="send_message"):
        from cold_outreach.core.agents.outreach import OutreachDecision
        from cold_outreach.emails.steps.follow_up import send_follow_up

        with patch("cold_outreach.core.agents.outreach.run_outreach_agent",
                   return_value=OutreachDecision(
                       action=action, message=message,
                       outcome="wrong_fit" if action == "mark_completed" else None),
                   ) as agent, \
                patch("cold_outreach.emails.sender.send_email") as send:
            next_state = send_follow_up(deal)
        return agent, send, next_state

    def test_it_goes_out_in_the_same_thread_under_the_same_subject(self, campaign, operator):
        """They never wrote back, so there is nothing to be replying to — no "Re:"."""
        deal = _pursued(campaign, touches=1, days_ago=10)

        _, send, _ = self._send(deal)

        kwargs = send.call_args.kwargs
        assert send.call_args.args[2] == "Hi there"
        assert kwargs["thread"] == deal.thread
        assert kwargs["in_reply_to"]

    def test_it_reuses_the_prompt_line_the_opener_used(self, campaign, operator):
        """One voice across the sequence, and one line to attribute a reply to."""
        from cold_outreach.core.prompt_lines import choose

        deal = _pursued(campaign, touches=0, days_ago=10)
        line = choose("plain-ask")
        maillog.outbound(deal.mailbox, thread=deal.thread, message_id="op@infra.com",
                         sent_at=_monday() - timedelta(days=10))
        deal.thread.messages.update(prompt_line_id=line.id, prompt_line_digest=line.digest)

        agent, send, _ = self._send(deal)

        assert agent.call_args.args[1].id == "plain-ask"
        assert send.call_args.kwargs["prompt_line"].id == "plain-ask"

    def test_it_asks_the_agent_for_a_follow_up_not_an_opener(self, campaign, operator):
        deal = _pursued(campaign, touches=1, days_ago=10)

        agent, _, _ = self._send(deal)

        assert agent.call_args.kwargs["stage"] == "follow_up"

    def test_a_lead_suppressed_mid_run_is_not_chased(self, campaign, operator):
        """The same last gate the opener has — a bounce or an opt-out may have landed."""
        from cold_outreach.leads.suppression import suppress_email

        deal = _pursued(campaign, touches=1, days_ago=10)
        suppress_email(deal.lead.email, reason="opted out")

        _, send, next_state = self._send(deal)

        send.assert_not_called()
        assert next_state is None

    def test_the_agent_may_end_it_instead_of_writing(self, campaign, operator):
        """Chasing somebody the record shows is wrong is worse than stopping."""
        deal = _pursued(campaign, touches=1, days_ago=10)

        _, send, next_state = self._send(deal, action="mark_completed")

        send.assert_not_called()
        assert next_state == DealState.COMPLETED
        assert deal.outcome == "wrong_fit"

    def test_the_box_is_spaced_out_afterwards(self, campaign, operator):
        """A follow-up is cold volume: it pays the same spacing an opener does."""
        deal = _pursued(campaign, touches=1, days_ago=10)
        assert deal.mailbox.next_send_at is None

        self._send(deal)

        deal.mailbox.refresh_from_db()
        assert deal.mailbox.next_send_at is not None
