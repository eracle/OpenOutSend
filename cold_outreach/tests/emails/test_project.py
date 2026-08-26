# tests/emails/test_project.py
"""**project** — acting on a reading, and only on a reading.

Two incidents live here. A bounce that reached nothing left ``SendVerdict`` with 0
rows against 590 sends, so capacity ramped while the domain bounced itself onto a
blocklist. And an NDR read as a conversation had the agent apologise to a dead
address twice.
"""
from unittest.mock import patch

import pytest

from cold_outreach.emails.classify import classify_pending
from cold_outreach.emails.models import DeliveryEvent, Kind, Message
from cold_outreach.emails.project import project_pending
from cold_outreach.emails.sync import mirror
from tests.emails import maillog
from tests.emails.fake_imap import FakeIMAP, bounce, message
from tests.factories import LeadFactory

SENDER = "s@infra.com"


def _box():
    return maillog.mailbox(SENDER)


def _pass(box, *rows):
    """One full mail pass over *rows*."""
    with patch("cold_outreach.emails.sync._connect", return_value=FakeIMAP(list(rows))):
        mirror(box)
    classify_pending()
    return project_pending()


@pytest.mark.django_db
class TestBounces:
    def test_a_bounce_becomes_a_delivery_event_against_the_send(self):
        box = _box()
        sent = maillog.outbound(box, to="p@corp.com", message_id="root@infra.com")

        _pass(box, bounce(7, to=SENDER, original="<root@infra.com>"))

        event = DeliveryEvent.objects.get(message=sent)
        assert event.status == DeliveryEvent.Status.BOUNCED
        assert event.enhanced_status == "5.1.1"
        assert event.reported_by.kind == Kind.BOUNCE

    def test_a_bounce_is_never_a_turn_in_the_conversation(self):
        """The deal-95 loop, closed by the schema rather than by a filter."""
        box = _box()
        sent = maillog.outbound(box, to="p@corp.com", message_id="root@infra.com")

        _pass(box, bounce(7, to=SENDER, original="<root@infra.com>"))

        thread = sent.thread
        assert thread.messages.count() == 2      # the send and the report
        assert list(thread.turns()) == [sent]    # only one of them was said

    def test_a_bounce_naming_nothing_we_know_still_leaves_its_row(self):
        box = _box()

        _pass(box, bounce(7, to=SENDER, original="<never-sent@infra.com>"))

        assert DeliveryEvent.objects.count() == 0
        assert Message.objects.get(kind=Kind.BOUNCE).processed_at is not None


@pytest.mark.django_db
class TestOptOuts:
    def test_the_alias_suppresses_everyone_holding_the_address(self):
        box = _box()
        first = LeadFactory(email="p@corp.com")
        second = LeadFactory(email="P@Corp.com")   # same person, different casing

        _pass(box, message(7, to="s+unsub@infra.com", sender="p@corp.com"))

        for lead in (first, second):
            lead.refresh_from_db()
            assert lead.disqualified


@pytest.mark.django_db
class TestPendingIsAState:
    def test_a_message_is_processed_or_pending_never_silently_neither(self):
        box = _box()
        maillog.outbound(box, to="p@corp.com")

        with patch("cold_outreach.emails.sync._connect",
                   return_value=FakeIMAP([message(7, to=SENDER, sender="p@corp.com")])):
            mirror(box)

        row = Message.objects.get(direction="in")
        assert row.is_pending                     # held, not yet interpreted
        classify_pending()
        project_pending()
        row.refresh_from_db()
        assert not row.is_pending

    def test_projection_is_not_redone_for_a_message_already_handled(self):
        box = _box()
        maillog.outbound(box, to="p@corp.com")
        assert _pass(box, message(7, to=SENDER, sender="p@corp.com")) == 1
        assert project_pending() == 0


@pytest.mark.django_db
class TestReporting:
    """The delivery question that was previously answerable only over live IMAP."""

    def test_bounce_rate_is_bounces_over_accepted_sends(self):
        from cold_outreach.emails.report import bounce_rate

        box = _box()
        for i in range(10):
            send = maillog.outbound(box, message_id=f"s{i}@infra.com")
            maillog.accepted(send)
            if i < 2:
                maillog.bounced(send)

        assert bounce_rate(box) == pytest.approx(0.2)

    def test_bounce_rate_of_a_box_that_never_sent_is_zero(self):
        from cold_outreach.emails.report import bounce_rate

        assert bounce_rate(_box()) == 0.0
