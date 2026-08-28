# cold_outreach/tests/emails/test_unsubscribe.py
"""Opt-out: the advertised mechanism, both detection paths, and the enforcement.

The three legs, and what each one locks down:

  * **Advertised** — every send carries the ``List-Unsubscribe`` header *and* a
    visible reply-line. The header is what receiving filters read; the line
    reaches the clients that render no unsubscribe button of their own. Both are
    asserted on the same message, since either alone leaves someone with no exit
    but the spam button.
  * **Detected** — a client-generated unsubscribe (no threading headers, found
    box-wide by the ``+unsub`` alias during the mail pass) and a worded one
    (threads normally, read by the outreach agent) both reach the same suppression.
  * **Enforced** — the address is on the list before anything is written, and the
    send path asks the list again after the agent has written.

What the list itself does once an address reaches it — which deals close, what a
second suppression changes — is ``tests/leads/test_suppression.py``.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from cold_outreach.core.agents.outreach import OutreachDecision
from cold_outreach.emails.mail_pass import run_mail_pass
from cold_outreach.emails.models import Mailbox
from cold_outreach.emails.sender import (
    ATTRIBUTION,
    OPT_OUT_LINE,
    send_email,
    suppressed,
    unsubscribe_address,
)
from cold_outreach.emails.steps.reply import answer_reply
from cold_outreach.emails.steps.send import send_first_email
from cold_outreach.leads.models import DealState, Lead, Outcome, Suppression
from cold_outreach.leads.suppression import suppress_email
from cold_outreach.tests.emails import maillog
from cold_outreach.tests.emails.fake_imap import FakeIMAP, message
from cold_outreach.tests.factories import DealFactory, LeadFactory

SENDER = "s@infra.com"
ALIAS = "s+unsub@infra.com"


def _box(**kwargs) -> Mailbox:
    return maillog.mailbox(SENDER, daily_limit=10, **kwargs)


def _sent_message(**kwargs):
    """The assembled EmailMessage for one send, without touching SMTP."""
    box = maillog.mailbox(SENDER, signature="Eracle")
    with patch("cold_outreach.emails.sender._deliver") as deliver:
        send_email(box, "lead@corp.com", "Hi", "Body", **kwargs)
    return deliver.call_args.args[1]


# ── The advertised mechanism ──────────────────────────────────────


@pytest.mark.django_db
class TestOptOutIsAdvertised:
    def test_opener_carries_the_list_unsubscribe_header(self):
        assert _sent_message()["List-Unsubscribe"] == f"<mailto:{ALIAS}?subject=unsubscribe>"

    def test_follow_up_carries_it_too(self):
        """Threaded replies go through the same assembly, so they carry it too."""
        message = _sent_message(in_reply_to="<prior@corp.com>")
        assert message["List-Unsubscribe"] == f"<mailto:{ALIAS}?subject=unsubscribe>"

    def test_no_list_unsubscribe_post_header(self):
        """One-click is only valid alongside an https: URI — asserting it absent
        stops a future edit adding the header without the endpoint."""
        assert _sent_message()["List-Unsubscribe-Post"] is None

    def test_body_carries_the_visible_opt_out_line(self):
        """The header reaches the filters; this reaches the clients that don't
        render an unsubscribe button of their own."""
        assert OPT_OUT_LINE in _sent_message().get_content()

    def test_opt_out_sits_between_the_signature_and_the_attribution(self):
        body = _sent_message().get_content()
        assert body.index("Eracle") < body.index(OPT_OUT_LINE) < body.index(ATTRIBUTION)


class TestUnsubscribeAddress:
    def test_alias_is_plus_addressed_on_the_sending_box(self):
        assert unsubscribe_address("s@infra.com") == "s+unsub@infra.com"

    def test_existing_plus_tag_is_not_disturbed(self):
        assert unsubscribe_address("s+out@infra.com") == "s+out+unsub@infra.com"


# ── Detection: the client-generated unsubscribe (the mail pass) ───


def _read(box, fake) -> int:
    """Run one mail pass against *fake*; return the addresses it suppressed."""
    before = Suppression.objects.count()
    with patch("cold_outreach.emails.sync._connect", return_value=fake):
        run_mail_pass()
    return Suppression.objects.count() - before


@pytest.mark.django_db
class TestAliasOptOut:
    """A client's unsubscribe button mints a fresh message with no threading
    headers at all, so the thread reader can never see it. The alias is the
    whole signal."""

    def test_an_alias_message_suppresses_its_sender(self, campaign):
        lead = LeadFactory(email="p@corp.com")
        deal = DealFactory(campaign=campaign, lead=lead, state=DealState.EMAILED)

        assert _read(_box(), FakeIMAP([message(7, to=ALIAS, sender="p@corp.com")])) == 1

        deal.refresh_from_db()
        assert suppressed(lead)
        assert deal.state == DealState.UNSUBSCRIBED

    def test_ordinary_inbox_mail_is_left_alone(self, campaign):
        lead = LeadFactory(email="p@corp.com")
        assert _read(_box(), FakeIMAP([message(7, to=SENDER, sender="p@corp.com")])) == 0
        assert not suppressed(lead)

    def test_a_display_name_around_the_alias_still_matches(self, campaign):
        LeadFactory(email="p@corp.com")
        to = f'"Unsubscribe" <{ALIAS}>'
        assert _read(_box(), FakeIMAP([message(7, to=to, sender="P@corp.com")])) == 1

    def test_the_opt_out_is_recorded_as_a_message_of_its_own(self, campaign):
        """It is a fact about the box before it is a decision about a person."""
        from cold_outreach.emails.models import Kind, Message

        LeadFactory(email="p@corp.com")
        _read(_box(), FakeIMAP([message(7, to=ALIAS, sender="p@corp.com")]))

        row = Message.objects.get(direction="in")
        assert row.kind == Kind.OPT_OUT
        assert row.processed_at is not None

    def test_rereading_the_same_message_changes_nothing(self, campaign):
        """Re-reading a box must be free — the log is keyed on the Message-ID."""
        lead = LeadFactory(email="p@corp.com")
        deal = DealFactory(campaign=campaign, lead=lead, state=DealState.EMAILED)
        box = _box()
        fake = FakeIMAP([message(7, to=ALIAS, sender="p@corp.com")])

        assert _read(box, fake) == 1
        coverage = box.coverage.get()
        coverage.last_uid = 0            # as a UIDVALIDITY change would leave it
        coverage.save(update_fields=["last_uid"])
        assert _read(box, fake) == 0     # already stored, already honoured

        deal.refresh_from_db()
        assert deal.state == DealState.UNSUBSCRIBED
        assert Suppression.objects.count() == 1

    def test_an_unreachable_box_keeps_its_coverage(self, campaign):
        """A network fault is not evidence that there was no mail to read."""
        box = _box()
        _read(box, FakeIMAP([message(7, to=SENDER, sender="x@corp.com")]))

        fake = FakeIMAP([])
        fake.login = MagicMock(side_effect=OSError("connection reset"))
        assert _read(box, fake) == 0
        assert box.coverage.get().last_uid == 7


# ── Detection: the worded unsubscribe (the outreach agent) ────────


def _decision(action, **kwargs):
    return OutreachDecision(action=action, **kwargs)


def _replied_deal(campaign, email="p@corp.com"):
    """An EMAILED deal with an unanswered reply — what the reply step picks up."""
    from datetime import timedelta

    box = _box()
    sent = maillog.outbound(box, to=email, message_id="root@infra.com",
                            sent_at=timezone.now() - timedelta(hours=2))
    maillog.inbound(box, thread=sent.thread, sender=email, body="Please stop.")
    return DealFactory(
        campaign=campaign,
        lead=LeadFactory(email=email),
        state=DealState.EMAILED,
        mailbox=box,
        email_subject="Hi",
        thread=sent.thread,
    )


@pytest.mark.django_db
class TestWordedUnsubscribe:
    """A worded unsubscribe threads normally, so the alias scan can never see it —
    the agent reading every reply already can."""

    def test_a_suppress_decision_lists_the_address_and_closes_the_deal(self, campaign):
        deal = _replied_deal(campaign)

        with patch("cold_outreach.core.agents.outreach.run_outreach_agent",
                   return_value=_decision("suppress")), \
             patch("cold_outreach.leads.summaries.update_chat_summary"), \
             patch("cold_outreach.emails.sender.send_email") as send:
            next_state = answer_reply(deal)

        deal.state = next_state
        deal.save()
        deal.refresh_from_db()
        assert deal.state == DealState.UNSUBSCRIBED
        assert deal.outcome == Outcome.UNSUBSCRIBED
        assert suppressed(deal.lead)
        send.assert_not_called()

    def test_the_outcome_alone_is_honoured_as_a_suppression(self, campaign):
        """`suppress` and the `unsubscribed` outcome are one decision worded two ways.
        Believing only the action would let an agent that read the reply correctly
        close the deal `COMPLETED` and leave the address sendable."""
        deal = _replied_deal(campaign)

        with patch("cold_outreach.core.agents.outreach.run_outreach_agent",
                   return_value=_decision("mark_completed", outcome="unsubscribed")), \
             patch("cold_outreach.leads.summaries.update_chat_summary"), \
             patch("cold_outreach.emails.sender.send_email") as send:
            next_state = answer_reply(deal)

        assert next_state == DealState.UNSUBSCRIBED
        assert suppressed(deal.lead)
        send.assert_not_called()

    def test_an_ordinary_ending_still_completes(self, campaign):
        """The reading above must not swallow the other seven outcomes."""
        deal = _replied_deal(campaign)

        with patch("cold_outreach.core.agents.outreach.run_outreach_agent",
                   return_value=_decision("mark_completed", outcome="not_interested")), \
             patch("cold_outreach.leads.summaries.update_chat_summary"):
            assert answer_reply(deal) == DealState.COMPLETED

        assert not suppressed(deal.lead)

    def test_the_deal_closes_even_when_the_address_matches_nothing(self, campaign):
        """``suppress_email`` is keyed on the address, the returned state on the
        deal. A lead with no address would otherwise stay EMAILED with an unanswered
        reply — permanently actionable, re-decided every pass."""
        deal = _replied_deal(campaign)
        Lead.objects.filter(pk=deal.lead.pk).update(email="")

        with patch("cold_outreach.core.agents.outreach.run_outreach_agent",
                   return_value=_decision("suppress")), \
             patch("cold_outreach.leads.summaries.update_chat_summary"):
            assert answer_reply(deal) == DealState.UNSUBSCRIBED


# ── Enforcement at the send call sites ───────────────────────────


@pytest.mark.django_db
class TestSendGuards:
    def test_suppressed_reads_the_list_not_the_in_memory_copy(self, campaign):
        lead = LeadFactory(email="p@corp.com")
        suppress_email("p@corp.com")
        assert suppressed(lead)  # the copy selected before the opt-out landed

    def test_a_first_email_is_not_sent_to_a_lead_suppressed_mid_run(self, campaign):
        """The agent runs for seconds — the query that selected this deal is
        already out of date by the time there is a message to send."""
        box = _box()
        deal = DealFactory(
            campaign=campaign,
            lead=LeadFactory(email="p@corp.com"),
            state=DealState.READY,
        )

        def _suppress_then_decide(target, prompt_line=None):
            suppress_email(target.lead.email)
            return _decision("send_message", subject="Hi", message="Body")

        with patch("cold_outreach.core.agents.outreach.run_outreach_agent",
                   side_effect=_suppress_then_decide), \
             patch("cold_outreach.leads.summaries.materialize_profile_summary_if_missing"), \
             patch("cold_outreach.emails.sender.send_email") as send:
            assert send_first_email(deal, box, None) is None

        send.assert_not_called()
        deal.refresh_from_db()
        assert deal.state == DealState.UNSUBSCRIBED

    def test_a_reply_is_not_sent_to_a_lead_suppressed_mid_run(self, campaign):
        deal = _replied_deal(campaign)

        def _suppress_then_decide(target):
            suppress_email(target.lead.email)
            return _decision("send_message", message="Body")

        with patch("cold_outreach.core.agents.outreach.run_outreach_agent",
                   side_effect=_suppress_then_decide), \
             patch("cold_outreach.leads.summaries.update_chat_summary"), \
             patch("cold_outreach.emails.sender.send_email") as send:
            assert answer_reply(deal) is None

        # `None` is the step declining to move the deal — the suppression already
        # closed it on its way onto the list, so there is nothing left to transition.
        send.assert_not_called()
        deal.refresh_from_db()
        assert deal.state == DealState.UNSUBSCRIBED
