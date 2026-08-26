# tests/emails/test_classify.py
"""**classify** — a pure, versioned reading of bytes we already hold.

The property that matters most is not any single rule: it is that a wrong rule is
*repairable*. Bump the version, re-run, and history is corrected — which is only
possible because reading and deciding are no longer the same act.
"""
from unittest.mock import patch

import pytest

from cold_outreach.emails import classify as classifier
from cold_outreach.emails.classify import CLASSIFIER_VERSION, classify, classify_pending
from cold_outreach.emails.models import Kind, Message
from cold_outreach.emails.sync import mirror
from cold_outreach.tests.emails import maillog
from cold_outreach.tests.emails.fake_imap import FakeIMAP, auto_reply, bounce, message

SENDER = "s@infra.com"


def _box():
    return maillog.mailbox(SENDER)


def _mirror(box, *rows):
    with patch("cold_outreach.emails.sync._connect", return_value=FakeIMAP(list(rows))):
        mirror(box)


def _kind_of(box, row) -> str:
    _mirror(box, row)
    classify_pending()
    return Message.objects.filter(direction="in").order_by("-pk").first().kind


@pytest.mark.django_db
class TestKinds:
    def test_a_reply_into_a_thread_we_opened_is_a_human_reply(self):
        box = _box()
        maillog.outbound(box, to="p@corp.com", message_id="root@infra.com")

        assert _kind_of(box, message(7, to=SENDER, sender="p@corp.com",
                                     references="<root@infra.com>")) == Kind.HUMAN_REPLY

    def test_a_bounce_is_never_a_reply(self):
        """It threads, it is auto-submitted, and it quotes our headers — asked first."""
        box = _box()
        maillog.outbound(box, to="p@corp.com", message_id="root@infra.com")

        assert _kind_of(box, bounce(7, to=SENDER,
                                    original="<root@infra.com>")) == Kind.BOUNCE

    def test_an_out_of_office_is_not_a_turn(self):
        box = _box()
        maillog.outbound(box, to="p@corp.com", message_id="root@infra.com")

        assert _kind_of(box, auto_reply(7, to=SENDER, sender="p@corp.com",
                                        original="<root@infra.com>")) == Kind.AUTO_REPLY

    def test_the_unsub_alias_is_an_opt_out(self):
        box = _box()

        assert _kind_of(box, message(7, to="s+unsub@infra.com",
                                     sender="p@corp.com")) == Kind.OPT_OUT

    def test_mail_outside_any_thread_we_started_is_unrelated(self):
        """Stored (we wrote to them once) but not a turn in any conversation."""
        box = _box()
        maillog.outbound(box, to="p@corp.com", message_id="root@infra.com")

        fresh = message(7, to=SENDER, sender="p@corp.com",
                        message_id="<brand-new@corp.com>", subject="Unrelated ask")
        assert _kind_of(box, fresh) == Kind.UNRELATED

    def test_our_own_message_arriving_back_is_not_a_reply(self):
        box = _box()
        maillog.outbound(box, to="p@corp.com", message_id="root@infra.com")

        assert _kind_of(box, message(7, to="p@corp.com", sender=SENDER,
                                     references="<root@infra.com>")) != Kind.HUMAN_REPLY

    def test_the_body_is_derived_with_the_quoted_history_stripped(self):
        box = _box()
        maillog.outbound(box, to="p@corp.com", message_id="root@infra.com")
        _mirror(box, message(7, to=SENDER, sender="p@corp.com", references="<root@infra.com>",
                             body="My answer.\r\n\r\nOn Mon, Eracle wrote:\r\n> the opener"))

        classify_pending()

        row = Message.objects.get(direction="in")
        assert row.body_text == "My answer."


@pytest.mark.django_db
class TestVersioning:
    def test_classified_rows_are_not_reread_at_the_same_version(self):
        box = _box()
        maillog.outbound(box, to="x@y.com")
        _mirror(box, message(7, to=SENDER, sender="x@y.com"))

        assert classify_pending() == 1
        assert classify_pending() == 0

    def test_a_new_version_re_reads_every_message_ever_received(self):
        box = _box()
        maillog.outbound(box, to="x@y.com")
        _mirror(box, message(7, to=SENDER, sender="x@y.com"))
        classify_pending()

        with patch.object(classifier, "CLASSIFIER_VERSION", CLASSIFIER_VERSION + 1):
            # Both rows: the reply and the send it answers. Everything behind the
            # current version is re-read, which is what makes history repairable.
            assert classify_pending() == 2

    def test_a_corrected_verdict_makes_the_projection_owed_again(self):
        """Repairing history, not only future mail: the row goes back to pending."""
        box = _box()
        maillog.outbound(box, to="p@corp.com", message_id="root@infra.com")
        _mirror(box, message(7, to=SENDER, sender="p@corp.com", references="<root@infra.com>"))
        classify_pending()

        row = Message.objects.get(direction="in")
        row.processed_at = row.classified_at
        row.kind = Kind.UNRELATED            # the wrong verdict of an older rule
        row.save(update_fields=["processed_at", "kind"])

        assert classify(row) == Kind.HUMAN_REPLY
        row.refresh_from_db()
        assert row.processed_at is None

    def test_a_row_with_no_bytes_keeps_the_verdict_it_was_written_with(self):
        """Backfilled history: you cannot re-read what was never kept."""
        box = _box()
        row = maillog.inbound(box, kind=Kind.HUMAN_REPLY)
        row.classifier_version = 0
        row.save(update_fields=["classifier_version"])

        classify_pending()

        row.refresh_from_db()
        assert row.kind == Kind.HUMAN_REPLY
