# openoutsend/tests/emails/test_mail_pass.py
"""The three jobs end to end: a reply arrives and the deal's conversation has a message waiting.

Everything here goes through ``run_mail_pass`` rather than the jobs individually,
because the property under test spans them — mail lands, is read, and only then
means something. The opt-out half of the same pass is in ``test_unsubscribe.py``.
"""
from datetime import timedelta
from unittest.mock import patch

import pytest

from openoutsend.emails.mail_pass import run_mail_pass
from openoutsend.emails.models import Kind, Message, Thread
from openoutsend.leads.lifecycle import machine_of
from openoutsend.leads.states import CONVERSING, awaiting, machine_name
from openoutsend.planner import conversations_waiting
from openoutsend.tests.emails import maillog
from openoutsend.tests.emails.fake_imap import (
    RECEIVED_AT,
    FakeIMAP,
    auto_reply,
    bounce,
    message,
)
from openoutsend.tests.factories import DealFactory, LeadFactory

SENDER = "s@infra.com"
ROOT = "root@infra.com"


def _emailed(box, email="p@corp.com", root=ROOT, state=CONVERSING):
    """A deal whose every step has gone out — the state a reply is answered in.

    Sent the day before the fake inbox's mail, so "their newest is newer than
    ours" is a fact about the thread rather than about test timing. The thread is
    kept against the deal, as the send that opens one keeps it.
    """
    sent = maillog.outbound(box, to=email, message_id=root,
                            sent_at=RECEIVED_AT - timedelta(days=1))
    deal = DealFactory(
        lead=LeadFactory(email=email),
        state=state,
        mailbox=box,
        email_subject="Hi",
        thread=sent.thread,
    )
    Thread.objects.filter(pk=sent.thread_id).update(
        machine=machine_name(deal.sequence), entity=str(deal.pk))
    return deal


def _pass(box, *rows):
    with patch("openoutsend.emails.sync._connect", return_value=FakeIMAP(list(rows))):
        return run_mail_pass()


@pytest.mark.django_db
class TestAReplyReachesItsDeal:
    def test_by_references(self):
        box = maillog.mailbox(SENDER)
        deal = _emailed(box)

        _pass(box, message(7, to=SENDER, sender="p@corp.com", references=f"<{ROOT}>",
                           body="Sure, happy to chat."))

        assert list(conversations_waiting()) == [deal]
        reply = Message.objects.get(direction="in")
        assert reply.kind == Kind.HUMAN_REPLY
        assert "happy to chat" in reply.body_text
        assert machine_of(deal).incoming(deal) == reply

    def test_one_that_arrives_before_the_conversation_waits_in_the_thread(self):
        """A reply never stops a step: it is a turn in the thread, answered once the
        deal converses."""
        box = maillog.mailbox(SENDER)
        deal = _emailed(box, state=awaiting(0))

        _pass(box, message(7, to=SENDER, sender="p@corp.com", references=f"<{ROOT}>"))

        reply = Message.objects.get(direction="in")
        assert (reply.kind, reply.thread_id) == (Kind.HUMAN_REPLY, deal.thread_id)
        assert deal.state == awaiting(0)
        assert list(conversations_waiting()) == []
        assert machine_of(deal).incoming(deal) == reply

    def test_by_in_reply_to_alone(self):
        """A client that fills only ``In-Reply-To`` points at the newest message,
        not the root — the reply root-matching dropped on the floor."""
        box = maillog.mailbox(SENDER)
        deal = _emailed(box)
        maillog.outbound(box, thread=deal.thread, to="p@corp.com",
                         message_id="second@infra.com",
                         sent_at=RECEIVED_AT - timedelta(hours=1))

        _pass(box, message(7, to=SENDER, sender="p@corp.com",
                           in_reply_to="<second@infra.com>"))

        assert list(conversations_waiting()) == [deal]

    def test_a_stranger_is_not_stored_and_makes_nothing_actionable(self):
        """The operator's own mail stays theirs: it is not our conversation."""
        box = maillog.mailbox(SENDER)
        _emailed(box)

        _pass(box, message(7, to=SENDER, sender="newsletter@x.com"))

        assert list(conversations_waiting()) == []
        assert Message.objects.filter(direction="in").count() == 0

    def test_a_thread_from_another_box_is_not_folded_in(self):
        """An id we sent from a different box cannot attach a reply to that thread."""
        from openoutsend.emails.classify import classify_pending
        from openoutsend.emails.project import project_pending
        from openoutsend.emails.sync import mirror

        box = maillog.mailbox(SENDER)
        other = maillog.mailbox("o@infra.com")
        _emailed(other)

        reply = FakeIMAP([message(7, to=SENDER, sender="p@corp.com",
                                  references=f"<{ROOT}>")])
        with patch("openoutsend.emails.sync._connect", return_value=reply):
            mirror(box)          # only this box sees the message
        classify_pending()
        project_pending()

        assert list(conversations_waiting()) == []

    def test_rereading_the_box_creates_no_duplicate(self):
        box = maillog.mailbox(SENDER)
        _emailed(box)
        reply = message(7, to=SENDER, sender="p@corp.com", references=f"<{ROOT}>")

        _pass(box, reply)
        coverage = box.coverage.get()
        coverage.last_uid = 0            # as a UIDVALIDITY change would leave it
        coverage.save(update_fields=["last_uid"])
        _pass(box, reply)

        assert Message.objects.filter(direction="in").count() == 1


@pytest.mark.django_db
class TestWhatIsNotAReply:
    def test_a_bounce_does_not_make_the_deal_actionable(self):
        """It arrives, it threads, and the agent is never handed it."""
        box = maillog.mailbox(SENDER)
        _emailed(box)

        _pass(box, bounce(7, to=SENDER, original=f"<{ROOT}>"))

        assert list(conversations_waiting()) == []

    def test_an_out_of_office_does_not_make_the_deal_actionable(self):
        box = maillog.mailbox(SENDER)
        _emailed(box)

        _pass(box, auto_reply(7, to=SENDER, sender="p@corp.com", original=f"<{ROOT}>"))

        assert list(conversations_waiting()) == []


@pytest.mark.django_db
class TestTheJobsAreIndependent:
    def test_an_unreachable_box_still_classifies_what_is_already_stored(self):
        """An outage delays reading the mail, not interpreting it."""
        box = maillog.mailbox(SENDER)
        deal = _emailed(box)
        _pass(box, message(7, to=SENDER, sender="p@corp.com", references=f"<{ROOT}>"))

        Message.objects.filter(direction="in").update(
            kind="", classifier_version=0, processed_at=None)
        with patch("openoutsend.emails.sync._connect", side_effect=OSError("no route")):
            mirrored, classified, projected = run_mail_pass()

        assert (mirrored, classified, projected) == (0, 1, 1)
        assert list(conversations_waiting()) == [deal]
