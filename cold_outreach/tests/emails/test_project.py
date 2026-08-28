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
from cold_outreach.leads.models import Deal, DealState, Outcome
from cold_outreach.leads.suppression import is_suppressed
from cold_outreach.tests.emails import maillog
from cold_outreach.tests.emails.fake_imap import FakeIMAP, bounce, message
from cold_outreach.tests.factories import DealFactory, LeadFactory

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
class TestUndeliverableAddresses:
    """The half of the bounce story that was missing: a dead address stops being mailed.

    `indieoutreach.app` reached SURBL with clean SPF/DKIM/DMARC — authentication was
    never the problem, address quality was, and nothing in the system could perceive
    it. Recording the bounce made it visible; these tests are what makes it act.
    """

    def _bounced(self, status, *, email="p@corp.com"):
        """One send to *email*, bounced back with *status*. Returns the deal."""
        box = _box()
        deal = DealFactory(lead=LeadFactory(email=email), state=DealState.EMAILED)
        maillog.outbound(box, to=email, message_id="root@infra.com")
        _pass(box, bounce(7, to=SENDER, original="<root@infra.com>",
                          recipient=email, status=status))
        deal.refresh_from_db()
        return deal

    def test_a_dead_address_is_suppressed_and_its_deal_ends_undeliverable(self):
        deal = self._bounced("5.1.1")

        assert is_suppressed("p@corp.com")
        assert (deal.state, deal.outcome) == (DealState.COMPLETED, Outcome.UNDELIVERABLE)

    def test_undeliverable_is_not_unsubscribed(self):
        """Nobody asked for anything — the funnel must not say they did."""
        assert self._bounced("5.1.1").outcome != Outcome.UNSUBSCRIBED

    @pytest.mark.parametrize("status", ["5.7.1", "5.7.26"])
    def test_a_reputation_block_never_suppresses_the_recipient(self, status):
        """5.7.x is about *this box*, not the person.

        Suppressing on it would delete good prospects from every future campaign for
        as long as our standing was poor — a recoverable dip turned into permanent,
        invisible list attrition.
        """
        deal = self._bounced(status)

        assert not is_suppressed("p@corp.com")
        assert deal.state == DealState.EMAILED

    @pytest.mark.parametrize("status", ["4.2.2", "5.2.2", None])
    def test_a_recoverable_failure_never_suppresses(self, status):
        """A deferral, a full mailbox, and a report naming no status at all.

        The mailbox empties, the deferral lifts, and an unreadable report is not
        evidence — so none of the three may end a pursuit.
        """
        deal = self._bounced(status)

        assert not is_suppressed("p@corp.com")
        assert deal.state == DealState.EMAILED

    def test_the_bounce_is_still_recorded_when_it_does_not_suppress(self):
        """Not suppressing is not the same as not noticing: warmth still reads it."""
        self._bounced("5.7.1")

        assert DeliveryEvent.objects.filter(status=DeliveryEvent.Status.BOUNCED).count() == 1

    def test_a_replacement_address_for_the_same_person_is_sendable(self):
        """Suppression is keyed on the address, so a corrected one is not caught by it."""
        self._bounced("5.1.1", email="old@corp.com")

        assert is_suppressed("old@corp.com")
        assert not is_suppressed("new@corp.com")

    def test_the_daemon_that_reported_it_is_never_the_one_suppressed(self):
        """The address to stop mailing is the one we wrote to, not the postmaster."""
        self._bounced("5.1.1")

        assert not is_suppressed("mailer-daemon@googlemail.com")


@pytest.mark.django_db
class TestOptOuts:
    def test_the_alias_suppresses_everyone_holding_the_address(self):
        box = _box()
        first = DealFactory(lead=LeadFactory(email="p@corp.com"))
        second = DealFactory(lead=LeadFactory(email="P@Corp.com"))   # same person, different casing

        _pass(box, message(7, to="s+unsub@infra.com", sender="p@corp.com"))

        assert is_suppressed("p@corp.com")
        for deal in (first, second):
            deal.refresh_from_db()
            assert (deal.state, deal.outcome) == (DealState.COMPLETED, Outcome.UNSUBSCRIBED)
        assert Deal.objects.exclude(outcome=Outcome.UNSUBSCRIBED).count() == 0


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
