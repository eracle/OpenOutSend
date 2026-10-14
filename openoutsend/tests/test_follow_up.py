"""Writing again to somebody who never answered.

Chasing was deleted once, for a reason that was really about *priority*: follow-ups
outranked openers out of a separate budget, and a live install measured 102 follow-ups
against 1 first email in a week. These tests hold the shape that makes that
unreachable — one budget, a cap on follow-ups, and a gap measured backwards from the
mail log rather than scheduled on a row — and the two rules sequences add: an install
can switch follow-ups off, and one that does not land before the next step is skipped.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from openoutsend.core.conf import FOLLOW_UP_GAPS_BUSINESS_DAYS
from openoutsend.emails.models import Thread
from openoutsend.leads.models import DealState, Outcome
from openoutsend.planner import awaiting_follow_up, exhausted_touches
from openoutsend.tests.emails import maillog
from openoutsend.tests.factories import DealFactory, LeadFactory

pytestmark = pytest.mark.django_db

# The opener plus every follow-up it may have.
ALL_TOUCHES = len(FOLLOW_UP_GAPS_BUSINESS_DAYS) + 1


def _monday(hour=10):
    """A fixed Monday, so a test never straddles a weekend by accident."""
    return timezone.now().replace(
        year=2026, month=3, day=16, hour=hour, minute=0, second=0, microsecond=0)


def _pursued(*, touches=1, days_ago=10, replied=False, box=None, sequence="cold"):
    """A deal at step 0 with *touches* sends, the last one *days_ago* days back.

    The first send is the step; the rest are its follow-ups.
    """
    box = box or maillog.mailbox()
    thread = Thread.objects.create(mailbox=box)
    deal = DealFactory(
        lead=LeadFactory(email="lead@acme.com"),
        state=DealState.EMAILED,
        thread=thread,
        mailbox=box,
        email_subject="Hi there",
        sequence=sequence,
        step=0,
    )
    when = _monday() - timedelta(days=days_ago)
    for index in range(touches):
        maillog.outbound(box, thread=thread, message_id=f"out{index}@infra.com",
                         sent_at=when - timedelta(days=touches - index - 1),
                         step=0 if index == 0 else None)
    if replied:
        maillog.inbound(box, thread=thread, body="Sure, tell me more",
                        sent_at=when + timedelta(hours=1))
    return deal


def _two_steps(tmp_path, days: int) -> str:
    """A sequence folder whose E1 is due *days* business days after E0."""
    (tmp_path / "E0-first.prompt.j2").write_text("Say hello.")
    (tmp_path / f"E1-{days}d-second.prompt.j2").write_text("Say hello again.")
    return str(tmp_path)


class TestWhoIsDue:
    def test_a_silent_lead_past_the_gap_is_due(self):
        _pursued(touches=1, days_ago=10)

        assert len(awaiting_follow_up(now=_monday())) == 1

    def test_a_lead_inside_the_gap_is_not(self):
        _pursued(touches=1, days_ago=1)

        assert awaiting_follow_up(now=_monday()) == []

    def test_a_lead_who_replied_is_never_chased(self):
        """Even after we answered them — a reply moves the thread out of this pool for good."""
        _pursued(touches=1, days_ago=10, replied=True)

        assert awaiting_follow_up(now=_monday()) == []

    def test_a_lead_out_of_follow_ups_is_not_due_but_is_exhausted(self):
        _pursued(touches=ALL_TOUCHES, days_ago=30)

        assert awaiting_follow_up(now=_monday()) == []
        assert len(exhausted_touches()) == 1

    def test_a_lead_with_a_follow_up_left_is_not_exhausted(self):
        _pursued(touches=ALL_TOUCHES - 1, days_ago=30)

        assert exhausted_touches() == []

    def test_the_gap_widens_with_each_touch(self):
        """The second gap is longer than the first, so touch three waits longer."""
        first, second = FOLLOW_UP_GAPS_BUSINESS_DAYS[:2]
        assert second > first

        # Waited long enough for the first gap, not for the second.
        _pursued(touches=2, days_ago=first + 1)

        assert awaiting_follow_up(now=_monday()) == []

    def test_a_weekend_does_not_count_toward_the_gap(self):
        """Friday → Monday is one working day, not three."""
        box = maillog.mailbox()
        thread = Thread.objects.create(mailbox=box)
        DealFactory(lead=LeadFactory(email="l@acme.com"),
                    state=DealState.EMAILED, thread=thread, mailbox=box, step=0)
        friday = _monday() - timedelta(days=3)
        maillog.outbound(box, thread=thread, message_id="out@infra.com", sent_at=friday, step=0)

        assert awaiting_follow_up(now=_monday()) == []


class TestTheSwitch:
    def test_off_means_nobody_is_chased(self, monkeypatch):
        monkeypatch.setenv("OUTSEND_FOLLOW_UPS", "off")
        _pursued(touches=1, days_ago=10)

        assert awaiting_follow_up(now=_monday()) == []

    def test_off_ends_the_pursuit_once_the_last_step_is_out(self, monkeypatch):
        """No follow-up is coming, so silence after the last step is the end of it."""
        monkeypatch.setenv("OUTSEND_FOLLOW_UPS", "off")
        _pursued(touches=1, days_ago=1)

        assert len(exhausted_touches()) == 1


class TestFittingBeforeTheNextStep:
    """``E0 ── 3d: follow-up 1 ── 5d: follow-up 2 ── E1``, and only what lands before E1."""

    def test_a_follow_up_that_lands_before_the_next_step_goes(self, tmp_path):
        sequence = _two_steps(tmp_path, days=10)
        _pursued(touches=1, days_ago=7, sequence=sequence)

        assert len(awaiting_follow_up(now=_monday(), sequence=sequence)) == 1

    def test_one_that_would_land_on_or_after_it_is_skipped(self, tmp_path):
        """E1 is due 3 business days after E0: the first follow-up is due then too, so it never goes."""
        sequence = _two_steps(tmp_path, days=3)
        _pursued(touches=1, days_ago=7, sequence=sequence)

        assert awaiting_follow_up(now=_monday(), sequence=sequence) == []

    def test_a_deal_with_a_step_still_to_come_is_not_exhausted(self, tmp_path):
        """Out of follow-ups, but E1 has not gone: the pursuit is not over."""
        sequence = _two_steps(tmp_path, days=20)
        _pursued(touches=ALL_TOUCHES, days_ago=30, sequence=sequence)

        assert exhausted_touches(sequence) == []


class TestGivingUp:
    def test_the_deal_completes_as_unresponsive(self):
        from openoutsend.emails.steps.follow_up import give_up

        deal = _pursued(touches=ALL_TOUCHES, days_ago=30)

        assert give_up(deal) == DealState.COMPLETED
        assert deal.outcome == "unresponsive"

    def test_silence_never_suppresses_the_address(self):
        """Not answering is not asking to stop — a later approach may still suit them."""
        from openoutsend.emails.steps.follow_up import give_up
        from openoutsend.leads.suppression import is_suppressed

        deal = _pursued(touches=ALL_TOUCHES, days_ago=30)
        give_up(deal)

        assert not is_suppressed(deal.lead.email)


class TestTheFollowUpItself:
    def _send(self, deal, message="One more thought.", action="send_message", outcome=None):
        from openoutsend.core.agents.outreach import OutreachDecision
        from openoutsend.emails.steps.follow_up import send_follow_up

        with patch("openoutsend.core.agents.outreach.run_outreach_agent",
                   return_value=OutreachDecision(
                       action=action, message=message,
                       outcome=outcome or ("wrong_fit" if action == "mark_completed" else None)),
                   ) as agent, \
                patch("openoutsend.emails.sender.send_email") as send:
            next_state = send_follow_up(deal)
        return agent, send, next_state

    def test_it_goes_out_in_the_same_thread_under_the_same_subject(self, operator):
        """They never wrote back, so there is nothing to be replying to — no "Re:"."""
        deal = _pursued(touches=1, days_ago=10)

        _, send, _ = self._send(deal)

        kwargs = send.call_args.kwargs
        assert send.call_args.args[2] == "Hi there"
        assert kwargs["thread"] == deal.thread
        assert kwargs["in_reply_to"]

    def test_it_is_written_from_the_follow_up_prompt_and_records_it(self, operator):
        """The prompt is the module's own, and it goes on the row so the next history replays it."""
        from openoutsend.emails.steps.follow_up import FOLLOW_UP_PROMPT

        deal = _pursued(touches=1, days_ago=10)

        agent, send, _ = self._send(deal)

        assert agent.call_args.args[1] == FOLLOW_UP_PROMPT
        assert send.call_args.kwargs["prompt"] == FOLLOW_UP_PROMPT
        # Not a step, and no move of its own: it keeps the opener's from the history.
        assert send.call_args.kwargs["step"] is None
        assert send.call_args.kwargs["prompt_line"] is None

    def test_a_lead_suppressed_mid_run_is_not_chased(self, operator):
        """The same last gate the opener has — a bounce or an opt-out may have landed."""
        from openoutsend.leads.suppression import suppress_email

        deal = _pursued(touches=1, days_ago=10)
        suppress_email(deal.lead.email, reason="opted out")

        _, send, next_state = self._send(deal)

        send.assert_not_called()
        assert next_state is None

    def test_the_agent_may_end_it_instead_of_writing(self, operator):
        """Chasing somebody the record shows is wrong is worse than stopping."""
        deal = _pursued(touches=1, days_ago=10)

        _, send, next_state = self._send(deal, action="mark_completed")

        send.assert_not_called()
        assert next_state == DealState.COMPLETED
        assert deal.outcome == "wrong_fit"

    def test_an_opt_out_here_is_honoured_rather_than_logged_away(self, operator):
        """A chase is written to silence, so the prompt never offers `suppress` — but a
        decision that reached for it used to fall through the "sending nothing" branch,
        leaving a deal the agent judged an opt-out chaseable again on the next pass."""
        from openoutsend.leads.suppression import is_suppressed

        deal = _pursued(touches=1, days_ago=10)

        _, send, next_state = self._send(deal, action="suppress")

        send.assert_not_called()
        assert (next_state, deal.outcome) == (DealState.COMPLETED, Outcome.UNSUBSCRIBED)
        assert is_suppressed(deal.lead.email)

    def test_naming_the_outcome_ends_it_the_same_way(self, operator):
        """The action and the outcome are one sentence: saying it in the other field
        cannot close a deal `COMPLETED` with the address still sendable."""
        from openoutsend.leads.suppression import is_suppressed

        deal = _pursued(touches=1, days_ago=10)

        _, send, next_state = self._send(deal, action="mark_completed", outcome="unsubscribed")

        send.assert_not_called()
        assert (next_state, deal.outcome) == (DealState.COMPLETED, Outcome.UNSUBSCRIBED)
        assert is_suppressed(deal.lead.email)

    def test_the_box_is_spaced_out_afterwards(self, operator):
        """A follow-up is cold volume: it pays the same spacing an opener does."""
        deal = _pursued(touches=1, days_ago=10)
        assert deal.mailbox.next_send_at is None

        self._send(deal)

        deal.mailbox.refresh_from_db()
        assert deal.mailbox.next_send_at is not None
