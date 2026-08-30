# cold_outreach/tests/emails/test_reply.py
"""Answering a reply.

A reply is the one thing that takes a lead out of the cold sequence for good: from
here the thread runs on what they write, and `leads/pools.awaiting_follow_up` can
never select the deal again. Chasing the silent ones is `test_follow_up.py`; this file
is what happens once somebody has actually said something.
"""
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from cold_outreach.core.agents.outreach import OutreachDecision
from cold_outreach.emails.models import Mailbox
from cold_outreach.emails.steps.reply import answer_reply
from cold_outreach.leads.models import DealState
from cold_outreach.leads.pools import unanswered_replies
from cold_outreach.tests.emails import maillog
from cold_outreach.tests.factories import DealFactory, LeadFactory

SENDER = "s@infra.com"


def _box() -> Mailbox:
    return maillog.mailbox(SENDER, daily_limit=10)


def _emailed(box, when=None, email="p@corp.com", root="root@infra.com"):
    """A deal we have emailed — a thread in the log with our opener in it."""
    when = when or timezone.now() - timedelta(days=3)
    sent = maillog.outbound(box, to=email, message_id=root, body="The opener.",
                            sent_at=when)
    return DealFactory(
        lead=LeadFactory(email=email),
        state=DealState.EMAILED,
        mailbox=box,
        email_subject="Hi",
        thread=sent.thread,
        email_sent_at=when,
    )


def _reply(deal, when=None, content="Sure, tell me more."):
    return maillog.inbound(deal.thread.mailbox, thread=deal.thread, body=content,
                           sent_at=when or timezone.now())


def _our_answer(deal, when=None):
    return maillog.outbound(deal.thread.mailbox, thread=deal.thread,
                            body="Answered.", sent_at=when or timezone.now())


# ── What makes a deal actionable ──────────────────────────────────


@pytest.mark.django_db
class TestUnansweredReplies:
    def test_silence_is_never_actionable(self):
        """**The core invariant.** No reply, no further email — ever. Not after a
        day, not after a month: an unanswered thread simply is not work."""
        box = _box()
        _emailed(box, when=timezone.now() - timedelta(days=90))

        assert list(unanswered_replies()) == []

    def test_a_reply_makes_the_deal_actionable(self):
        box = _box()
        deal = _emailed(box)
        _reply(deal)

        assert list(unanswered_replies()) == [deal]

    def test_a_thread_we_have_already_answered_is_not_actionable(self):
        box = _box()
        deal = _emailed(box)
        _reply(deal, when=timezone.now() - timedelta(hours=2))
        _our_answer(deal)

        assert list(unanswered_replies()) == []

    def test_a_second_reply_after_our_answer_reopens_it(self):
        box = _box()
        deal = _emailed(box)
        _reply(deal, when=timezone.now() - timedelta(hours=2))
        _our_answer(deal, when=timezone.now() - timedelta(hours=1))
        _reply(deal, when=timezone.now())

        assert list(unanswered_replies()) == [deal]

    def test_a_closed_deal_is_left_alone(self):
        box = _box()
        deal = _emailed(box)
        _reply(deal)
        deal.outcome = "not_interested"
        deal.save(update_fields=["outcome"])

        assert list(unanswered_replies()) == []

    def test_oldest_reply_first(self):
        box = _box()
        waiting = _emailed(box)
        _reply(waiting, when=timezone.now() - timedelta(hours=5))
        fresh = _emailed(box, email="q@corp.com", root="root2@infra.com")
        _reply(fresh, when=timezone.now())

        assert list(unanswered_replies()) == [waiting, fresh]

    def test_the_query_does_not_group_by(self):
        """The two timestamps must stay subqueries, never aggregates.

        An aggregate groups by every selected column, and ``select_related`` puts the
        lead and mailbox BLOBs (``model_blob`` above all) in that list — 6.5 GB
        for one ``.first()`` on a live install, and the daemon's OOM kill.
        """
        sql = str(unanswered_replies().query).upper()

        assert "GROUP BY" not in sql


# ── answer_reply (the step) ───────────────────────────────────────


@pytest.mark.django_db
class TestAnswerReply:
    def _run(self, deal, decision):
        with patch("cold_outreach.core.agents.outreach.run_outreach_agent",
                   return_value=decision), \
                patch("cold_outreach.leads.summaries.update_chat_summary"), \
                patch("cold_outreach.emails.sender.send_email",
                      side_effect=lambda box, *a, **kw: maillog.outbound(
                          box, thread=kw.get("thread"),
                          message_id="sent@infra.com")) as send:
            return send, answer_reply(deal)

    def test_a_reply_is_threaded_and_recorded(self, operator):
        box = _box()
        deal = _emailed(box)
        _reply(deal)

        send, next_state = self._run(
            deal, OutreachDecision(action="send_message", message="Glad to."))

        assert next_state is None  # stays EMAILED
        kwargs = send.call_args.kwargs
        # The whole chain, so a client threading on the root and one threading on
        # the newest message both find their anchor.
        assert "<root@infra.com>" in kwargs["references"]
        assert kwargs["in_reply_to"] == kwargs["references"].split()[-1]
        assert kwargs["thread"] == deal.thread
        assert send.call_args.args[2] == "Re: Hi"
        assert deal.thread.messages.filter(message_id="sent@infra.com").exists()

    def test_answering_makes_the_deal_quiet_again(self, operator):
        box = _box()
        deal = _emailed(box)
        _reply(deal)

        self._run(deal, OutreachDecision(action="send_message", message="Glad to."))

        assert list(unanswered_replies()) == []

    def test_a_reply_ignores_the_daily_cap(self, operator):
        """A reply is not cold volume, so a box at its ceiling still answers."""
        box = _box()
        deal = _emailed(box)
        _reply(deal)
        box.daily_limit = 0
        box.save(update_fields=["daily_limit"])

        send, _ = self._run(
            deal, OutreachDecision(action="send_message", message="Glad to."))

        send.assert_called_once()

    def test_a_reply_ignores_send_spacing(self, operator):
        box = _box()
        box.next_send_at = timezone.now() + timedelta(hours=1)
        box.save(update_fields=["next_send_at"])
        deal = _emailed(box)
        _reply(deal)

        send, _ = self._run(
            deal, OutreachDecision(action="send_message", message="Glad to."))

        send.assert_called_once()
        box.refresh_from_db()
        assert box.next_send_at > timezone.now()  # untouched by the reply

    def test_completing_carries_the_outcome(self, operator):
        box = _box()
        deal = _emailed(box)
        _reply(deal)

        send, next_state = self._run(deal, OutreachDecision(
            action="mark_completed", outcome="not_interested"))

        assert next_state == DealState.COMPLETED
        assert deal.outcome == "not_interested"
        send.assert_not_called()
