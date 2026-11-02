# tests/emails/test_filing.py
"""**file** — a message the headers could not place, put in the conversation it belongs to.

The caller knows whose answer it is (it sent the mail being answered); this side only
moves it, and the ordinary reading of a thread we sent into does the rest.
"""
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from openoutsend.emails.classify import classify_pending
from openoutsend.emails.filing import file_message
from openoutsend.emails.models import Kind, Message
from openoutsend.emails.steps.reply import send_drafted_reply
from openoutsend.emails.sync import mirror
from openoutsend.errors import OutsendError
from openoutsend.leads.models import DealState
from openoutsend.planner import unanswered_replies
from openoutsend.tests.emails import maillog
from openoutsend.tests.emails.fake_imap import FakeIMAP, auto_reply, message
from openoutsend.tests.factories import DealFactory, LeadFactory

SENDER = "s@infra.com"
CUSTOMER = "p@corp.com"


def _box():
    return maillog.mailbox(SENDER)


def _deal(box, *, lead=None, sequence="cold", created_at=None):
    """A deal we emailed before the customer's message arrived."""
    sent = maillog.outbound(box, to=CUSTOMER, sent_at=datetime(2026, 3, 1, tzinfo=UTC))
    return DealFactory(
        lead=lead or LeadFactory(lead_id="sample-7", email=CUSTOMER),
        state=DealState.EMAILED, mailbox=box, email_subject="Hi", thread=sent.thread,
        sequence=sequence, **({"created_at": created_at} if created_at else {}),
    )


def _received(box, *rows) -> Message:
    with patch("openoutsend.emails.sync._connect", return_value=FakeIMAP(list(rows))):
        mirror(box)
    classify_pending()
    return Message.objects.filter(direction="in").order_by("-pk").first()


@pytest.mark.django_db
class TestFiling:
    def test_an_answer_to_mail_we_did_not_send_joins_the_conversation(self):
        box = _box()
        deal = _deal(box)
        row = _received(box, message(7, to=SENDER, sender=CUSTOMER,
                                     in_reply_to="<sample@resend.dev>"))
        assert row.kind == Kind.UNRELATED

        filed = file_message("<m7@corp.com>", "sample-7")

        assert filed.kind == Kind.HUMAN_REPLY
        assert filed.thread_id == deal.thread_id
        assert list(unanswered_replies()) == [deal]

    def test_its_own_thread_moves_with_it(self):
        box = _box()
        deal = _deal(box)
        _received(box, message(7, to=SENDER, sender=CUSTOMER,
                               in_reply_to="<sample@resend.dev>"),
                  message(8, to=SENDER, sender=CUSTOMER, in_reply_to="<m7@corp.com>"))

        file_message("m8@corp.com", "sample-7")

        assert set(deal.thread.messages.filter(direction="in")
                   .values_list("kind", flat=True)) == {Kind.HUMAN_REPLY}
        assert deal.thread.messages.filter(direction="in").count() == 2

    def test_it_joins_the_newest_deal(self):
        box = _box()
        older = _deal(box, created_at=datetime(2026, 3, 1, tzinfo=UTC))
        newer = _deal(box, lead=older.lead, sequence="sample-to-order")
        _received(box, message(7, to=SENDER, sender=CUSTOMER))

        assert file_message("m7@corp.com", "sample-7").thread_id == newer.thread_id

    def test_a_re_reading_keeps_it_in_place(self):
        box = _box()
        deal = _deal(box)
        _received(box, message(7, to=SENDER, sender=CUSTOMER))
        filed = file_message("m7@corp.com", "sample-7")

        with patch("openoutsend.emails.classify.CLASSIFIER_VERSION", 99):
            classify_pending()

        filed.refresh_from_db()
        assert (filed.kind, filed.thread_id) == (Kind.HUMAN_REPLY, deal.thread_id)

    def test_the_answer_goes_in_reply_to_their_message(self, operator):
        box = _box()
        deal = _deal(box)
        _received(box, message(7, to=SENDER, sender=CUSTOMER))
        file_message("m7@corp.com", "sample-7")

        with patch("openoutsend.emails.sender.send_email") as send:
            send_drafted_reply(deal, "Thanks — here is your order link.")

        assert send.call_args.kwargs["in_reply_to"] == "<m7@corp.com>"
        assert send.call_args.args[2] == "Re: Hi"


@pytest.mark.django_db
class TestRefusals:
    def test_an_auto_reply_is_not_filed(self):
        box = _box()
        _deal(box)
        _received(box, auto_reply(7, to=SENDER, sender=CUSTOMER,
                                  original="<sample@resend.dev>"))

        with pytest.raises(OutsendError, match="auto_reply"):
            file_message("m7@corp.com", "sample-7")

    def test_an_opt_out_is_not_filed(self):
        box = _box()
        _deal(box)
        _received(box, message(7, to="s+unsub@infra.com", sender=CUSTOMER))

        with pytest.raises(OutsendError, match="opt_out"):
            file_message("m7@corp.com", "sample-7")

    def test_a_reply_already_in_a_conversation_is_not_filed_again(self):
        box = _box()
        _deal(box)
        _received(box, message(7, to=SENDER, sender=CUSTOMER))
        file_message("m7@corp.com", "sample-7")

        with pytest.raises(OutsendError, match="human_reply"):
            file_message("m7@corp.com", "sample-7")

    def test_a_lead_with_no_thread_has_nowhere_to_file_it(self):
        box = _box()
        _deal(box, lead=LeadFactory(lead_id="other", email="o@corp.com"))
        DealFactory(lead=LeadFactory(lead_id="sample-7", email=CUSTOMER))
        _received(box, message(7, to=SENDER, sender=CUSTOMER))

        with pytest.raises(OutsendError, match="no conversation"):
            file_message("m7@corp.com", "sample-7")

    def test_a_message_the_store_does_not_hold(self):
        with pytest.raises(OutsendError, match="no received message"):
            file_message("ghost@corp.com", "sample-7")
