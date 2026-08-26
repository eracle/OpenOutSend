# tests/emails/test_mail_pass.py
"""The three jobs end to end: a reply arrives and the deal becomes actionable.

Everything here goes through ``run_mail_pass`` rather than the jobs individually,
because the property under test spans them — mail lands, is read, and only then
means something. The opt-out half of the same pass is in ``test_unsubscribe.py``.
"""
from datetime import timedelta
from unittest.mock import patch

import pytest

from openoutreach.core.cycle import unanswered_replies
from openoutreach.crm.models import DealState
from cold_outreach.emails.mail_pass import run_mail_pass
from cold_outreach.emails.models import Kind, Message
from tests.emails import maillog
from tests.emails.fake_imap import RECEIVED_AT, FakeIMAP, auto_reply, bounce, message
from tests.factories import DealFactory, LeadFactory

SENDER = "s@infra.com"
ROOT = "root@infra.com"


def _emailed(campaign, box, email="p@corp.com", root=ROOT):
    """A deal whose opener has gone out — the state a reply arrives into.

    Sent the day before the fake inbox's mail, so "their newest is newer than
    ours" is a fact about the thread rather than about test timing.
    """
    sent = maillog.outbound(box, to=email, message_id=root,
                            sent_at=RECEIVED_AT - timedelta(days=1))
    return DealFactory(
        campaign=campaign,
        lead=LeadFactory(email=email),
        state=DealState.EMAILED,
        mailbox=box,
        email_subject="Hi",
        thread=sent.thread,
    )


def _pass(box, *rows):
    with patch("cold_outreach.emails.sync._connect", return_value=FakeIMAP(list(rows))):
        return run_mail_pass()


@pytest.mark.django_db
class TestAReplyReachesItsDeal:
    def test_by_references(self, campaign):
        box = maillog.mailbox(SENDER)
        deal = _emailed(campaign, box)

        _pass(box, message(7, to=SENDER, sender="p@corp.com", references=f"<{ROOT}>",
                           body="Sure, happy to chat."))

        assert list(unanswered_replies(campaign)) == [deal]
        reply = Message.objects.get(direction="in")
        assert reply.kind == Kind.HUMAN_REPLY
        assert "happy to chat" in reply.body_text

    def test_by_in_reply_to_alone(self, campaign):
        """A client that fills only ``In-Reply-To`` points at the newest message,
        not the root — the reply root-matching dropped on the floor."""
        box = maillog.mailbox(SENDER)
        deal = _emailed(campaign, box)
        maillog.outbound(box, thread=deal.thread, to="p@corp.com",
                         message_id="second@infra.com",
                         sent_at=RECEIVED_AT - timedelta(hours=1))

        _pass(box, message(7, to=SENDER, sender="p@corp.com",
                           in_reply_to="<second@infra.com>"))

        assert list(unanswered_replies(campaign)) == [deal]

    def test_a_stranger_is_not_stored_and_makes_nothing_actionable(self, campaign):
        """The operator's own mail stays theirs: it is not our conversation."""
        box = maillog.mailbox(SENDER)
        _emailed(campaign, box)

        _pass(box, message(7, to=SENDER, sender="newsletter@x.com"))

        assert list(unanswered_replies(campaign)) == []
        assert Message.objects.filter(direction="in").count() == 0

    def test_a_thread_from_another_box_is_not_folded_in(self, campaign):
        """An id we sent from a different box cannot attach a reply to that thread."""
        from cold_outreach.emails.classify import classify_pending
        from cold_outreach.emails.project import project_pending
        from cold_outreach.emails.sync import mirror

        box = maillog.mailbox(SENDER)
        other = maillog.mailbox("o@infra.com")
        _emailed(campaign, other)

        reply = FakeIMAP([message(7, to=SENDER, sender="p@corp.com",
                                  references=f"<{ROOT}>")])
        with patch("cold_outreach.emails.sync._connect", return_value=reply):
            mirror(box)          # only this box sees the message
        classify_pending()
        project_pending()

        assert list(unanswered_replies(campaign)) == []

    def test_rereading_the_box_creates_no_duplicate(self, campaign):
        box = maillog.mailbox(SENDER)
        _emailed(campaign, box)
        reply = message(7, to=SENDER, sender="p@corp.com", references=f"<{ROOT}>")

        _pass(box, reply)
        coverage = box.coverage.get()
        coverage.last_uid = 0            # as a UIDVALIDITY change would leave it
        coverage.save(update_fields=["last_uid"])
        _pass(box, reply)

        assert Message.objects.filter(direction="in").count() == 1


@pytest.mark.django_db
class TestWhatIsNotAReply:
    def test_a_bounce_does_not_make_the_deal_actionable(self, campaign):
        """It arrives, it threads, and the agent is never handed it."""
        box = maillog.mailbox(SENDER)
        _emailed(campaign, box)

        _pass(box, bounce(7, to=SENDER, original=f"<{ROOT}>"))

        assert list(unanswered_replies(campaign)) == []

    def test_an_out_of_office_does_not_make_the_deal_actionable(self, campaign):
        box = maillog.mailbox(SENDER)
        _emailed(campaign, box)

        _pass(box, auto_reply(7, to=SENDER, sender="p@corp.com", original=f"<{ROOT}>"))

        assert list(unanswered_replies(campaign)) == []


@pytest.mark.django_db
class TestTheJobsAreIndependent:
    def test_an_unreachable_box_still_classifies_what_is_already_stored(self, campaign):
        """An outage delays reading the mail, not interpreting it."""
        box = maillog.mailbox(SENDER)
        deal = _emailed(campaign, box)
        _pass(box, message(7, to=SENDER, sender="p@corp.com", references=f"<{ROOT}>"))

        Message.objects.filter(direction="in").update(
            kind="", classifier_version=0, processed_at=None)
        with patch("cold_outreach.emails.sync._connect", side_effect=OSError("no route")):
            mirrored, classified, projected = run_mail_pass()

        assert (mirrored, classified, projected) == (0, 1, 1)
        assert list(unanswered_replies(campaign)) == [deal]
