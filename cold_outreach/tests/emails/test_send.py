# tests/emails/test_send.py
"""The first-email path: the per-box cap and spacing, the ready pool, and the step.

A first email is the only cold volume this daemon produces, so it is the only send
under a cap — replies are exempt (see ``test_reply.py``)."""
import logging

import pytest
from unittest.mock import patch

from django.utils import timezone

from cold_outreach.core.agents.outreach import OutreachDecision
from openoutreach.core.db.deals import get_emailable_deals
from openoutreach.crm.models import DealState
from cold_outreach.emails.models import Mailbox
from cold_outreach.emails.sender import (
    ATTRIBUTION,
    OPT_OUT_LINE,
    operator_bcc,
    send_email,
    unsubscribe_address,
)
from cold_outreach.emails.models import Thread
from cold_outreach.emails.steps.send import send_first_email
from tests.emails import maillog
from tests.factories import DealFactory, LeadFactory


def _box(email="a@b.com", daily_limit=10):
    return maillog.mailbox(email, daily_limit=daily_limit)


def _ready(campaign, email="lead@corp.com"):
    """A deal queued for its Layer-1 email (READY_TO_EMAIL, address resolved)."""
    return DealFactory(
        campaign=campaign,
        lead=LeadFactory(email=email),
        state=DealState.READY_TO_EMAIL,
    )


def _record_send(deal, box, user, when=None):
    """Register one first contact on a box — what the cap ledger counts.

    The ledger reads the transport log, so this writes what a send writes: an
    outbound message opening a thread, and a deal pointing at that thread.
    """
    when = when or timezone.now()
    thread = Thread.objects.create(mailbox=box)
    maillog.outbound(box, thread=thread, to=deal.lead.email, sent_at=when)
    deal.mailbox = box
    deal.thread = thread
    deal.state = DealState.EMAILED
    deal.email_sent_at = when
    deal.save()
    return deal


def _record_reply_exchange(deal, box, user, when):
    """One inbound reply and our answer, both inside an existing thread."""
    maillog.inbound(box, thread=deal.thread, sent_at=when)
    maillog.outbound(box, thread=deal.thread, to=deal.lead.email, sent_at=when)


# ── Mailbox pacing ────────────────────────────────────────────────


@pytest.mark.django_db
class TestMailboxCap:
    """The cap counts *people first contacted today*, not messages sent today."""

    def test_a_first_email_spends_one(self, campaign, operator):
        box = _box(daily_limit=10)
        assert box.sent_today() == 0
        _record_send(_ready(campaign), box, operator)
        assert box.sent_today() == 1
        assert box.headroom_today() == 9

    def test_replies_inside_an_older_thread_are_free(self, campaign, operator):
        """Answering someone who wrote back is not cold volume, so it costs no cap."""
        from datetime import timedelta

        box = _box(daily_limit=10)
        yesterday = timezone.now() - timedelta(days=1)
        deal = _record_send(_ready(campaign), box, operator, when=yesterday)
        _record_reply_exchange(deal, box, operator, when=timezone.now())

        assert box.sent_today() == 0
        assert box.headroom_today() == 10

    def test_one_person_reached_twice_today_counts_once(self, campaign, operator):
        """Distinct leads, so a second campaign's touch is not a second person."""
        from openoutreach.core.models import Campaign

        box = _box(daily_limit=10)
        lead = LeadFactory(email="lead@corp.com")
        other = Campaign.objects.create(name="Second")
        for c in (campaign, other):
            _record_send(
                DealFactory(campaign=c, lead=lead, state=DealState.READY_TO_EMAIL),
                box, operator,
            )
        assert box.sent_today() == 1

    def test_remaining_today_sums_headroom_across_boxes(self, campaign):
        _box("a@b.com", daily_limit=3)
        _box("c@d.com", daily_limit=5)
        assert Mailbox.objects.remaining_today() == 8

    def test_remaining_today_zero_with_no_boxes(self):
        assert Mailbox.objects.remaining_today() == 0


@pytest.mark.django_db
class TestPickingABox:
    def test_picks_the_box_with_most_headroom(self, campaign, operator):
        light = _box("light@b.com", daily_limit=10)
        heavy = _box("heavy@b.com", daily_limit=10)
        for _ in range(4):
            _record_send(_ready(campaign), heavy, operator)
        assert Mailbox.objects.free_for_first_email() == light

    def test_none_when_every_box_is_capped(self, campaign, operator):
        box = _box(daily_limit=1)
        _record_send(_ready(campaign), box, operator)
        assert Mailbox.objects.free_for_first_email() is None

    def test_a_box_still_spacing_out_is_not_free(self, campaign):
        """The 3-minute floor between two cold emails, per box."""
        from datetime import timedelta

        box = _box(daily_limit=10)
        box.next_send_at = timezone.now() + timedelta(minutes=3)
        box.save(update_fields=["next_send_at"])
        assert Mailbox.objects.free_for_first_email() is None

    def test_a_box_past_its_spacing_is_free_again(self, campaign):
        from datetime import timedelta

        box = _box(daily_limit=10)
        box.next_send_at = timezone.now() - timedelta(seconds=1)
        box.save(update_fields=["next_send_at"])
        assert Mailbox.objects.free_for_first_email() == box

    def test_the_daily_ledger_starts_at_the_operators_midnight(self):
        """Not the server's UTC midnight, which for a US operator falls at 19:00 —
        inside the window, so the cap would reset an hour before the day ends."""
        from zoneinfo import ZoneInfo

        from cold_outreach.emails.models.mailbox import _local_midnight

        zone = ZoneInfo("America/New_York")
        with patch("cold_outreach.emails.models.mailbox.operator_timezone", return_value=zone):
            midnight = _local_midnight()
        assert midnight.astimezone(zone).hour == 0

    def test_no_box_is_free_outside_the_sending_window(self, campaign):
        """Out of hours nothing opens a conversation, however much headroom is left."""
        _box(daily_limit=10)
        with patch("cold_outreach.emails.models.mailbox.within_sending_window", return_value=False):
            assert Mailbox.objects.free_for_first_email() is None


# ── Email pool ────────────────────────────────────────────────────


@pytest.mark.django_db
class TestEmailableDeals:
    def test_returns_only_ready_to_email(self, campaign):
        ready = _ready(campaign)
        DealFactory(campaign=campaign, lead=LeadFactory(), state=DealState.QUALIFIED)
        DealFactory(campaign=campaign, lead=LeadFactory(), state=DealState.EMAILED)
        deals = list(get_emailable_deals(campaign))
        assert deals == [ready]

    def test_excludes_disqualified_lead(self, campaign):
        deal = _ready(campaign)
        deal.lead.disqualified = True
        deal.lead.save()
        assert list(get_emailable_deals(campaign)) == []

    def test_oldest_first(self, campaign):
        first = _ready(campaign, "first@c.com")
        second = _ready(campaign, "second@c.com")
        assert list(get_emailable_deals(campaign)) == [first, second]


@pytest.mark.django_db
class TestSendEmailBcc:
    def test_bcc_header_set_when_address_given(self):
        box = maillog.mailbox("s@infra.com")
        with patch("cold_outreach.emails.sender._deliver") as deliver:
            send_email(box, "lead@corp.com", "Hi", "Body", bcc="me@mine.com")
        message = deliver.call_args.args[1]
        assert message["Bcc"] == "me@mine.com"

    def test_no_bcc_header_when_address_blank(self):
        box = maillog.mailbox("s@infra.com")
        with patch("cold_outreach.emails.sender._deliver") as deliver:
            send_email(box, "lead@corp.com", "Hi", "Body", bcc="")
        message = deliver.call_args.args[1]
        assert message["Bcc"] is None


@pytest.mark.django_db
class TestSentBodyLogging:
    """The message text is logged for the operator's own campaigns, never freemium."""

    def _send(self, campaign, caplog):
        box = maillog.mailbox("s@infra.com", signature="— Ercole")
        with caplog.at_level(logging.INFO, logger="cold_outreach.emails.sender"), \
             patch("cold_outreach.emails.sender._deliver"):
            send_email(box, "lead@corp.com", "Hi there", "How do you do discovery today?",
                       campaign=campaign)
        return caplog.text

    def test_own_campaign_logs_what_the_agent_generated(self, campaign, caplog):
        text = self._send(campaign, caplog)
        assert "Subject: Hi there" in text
        assert "How do you do discovery today?" in text

    def test_the_appended_boilerplate_is_not_logged(self, campaign, caplog):
        """Signature, opt-out and attribution are the same on every send — noise here."""
        text = self._send(campaign, caplog)
        assert "— Ercole" not in text
        assert ATTRIBUTION.strip() not in text
        assert OPT_OUT_LINE.strip() not in text

    def test_freemium_campaign_logs_metadata_only(self, campaign, caplog):
        campaign.is_freemium = True
        text = self._send(campaign, caplog)
        assert "lead@corp.com" in text          # the metadata line still goes out
        assert "How do you do discovery today?" not in text

    def test_no_campaign_logs_metadata_only(self, caplog):
        """The default stays metadata-only, so a new call site cannot leak by omission."""
        box = maillog.mailbox("s@infra.com")
        with caplog.at_level(logging.INFO, logger="cold_outreach.emails.sender"), \
             patch("cold_outreach.emails.sender._deliver"):
            send_email(box, "lead@corp.com", "Hi there", "Secret body")
        assert "Secret body" not in caplog.text


@pytest.mark.django_db
class TestOperatorBcc:
    def test_operator_campaign_bccs_the_operator(self, campaign, operator):
        assert operator_bcc(operator, campaign) == "testuser@example.com"

    def test_freemium_campaign_never_bccs(self, campaign, operator):
        campaign.is_freemium = True
        assert operator_bcc(operator, campaign) is None

    def test_blank_operator_email_yields_no_bcc(self, campaign, operator):
        """An empty address would set an empty Bcc header, not "no copy"."""
        operator.email = ""
        assert operator_bcc(operator, campaign) is None


@pytest.mark.django_db
class TestSendEmailSignature:
    def _sent_body(self, signature: str | None) -> str:
        box = maillog.mailbox("s@infra.com", signature=signature)
        with patch("cold_outreach.emails.sender._deliver") as deliver:
            send_email(box, "lead@corp.com", "Hi", "Body")
        return deliver.call_args.args[1].get_content()

    def test_signature_appended_after_blank_line(self):
        body = self._sent_body("Eracle\nopenoutreach.app")
        assert body == (
            f"Body\n\nEracle\nopenoutreach.app\n\n{OPT_OUT_LINE}\n\n{ATTRIBUTION}\n"
        )

    def test_body_carries_only_opt_out_and_attribution_when_signature_blank(self):
        assert self._sent_body("") == f"Body\n\n{OPT_OUT_LINE}\n\n{ATTRIBUTION}\n"

    def test_body_carries_only_opt_out_and_attribution_when_signature_unset(self):
        """A never-asked box (NULL) sends unsigned rather than crashing on None."""
        assert self._sent_body(None) == f"Body\n\n{OPT_OUT_LINE}\n\n{ATTRIBUTION}\n"


@pytest.mark.django_db
class TestSendEmailAttribution:
    def _box(self, signature=None):
        return maillog.mailbox("s@infra.com", signature=signature)

    def _sent_body(self, box, **kwargs) -> str:
        with patch("cold_outreach.emails.sender._deliver") as deliver:
            send_email(box, "lead@corp.com", "Hi", "Body", **kwargs)
        return deliver.call_args.args[1].get_content()

    def test_attribution_is_the_last_line(self):
        body = self._sent_body(self._box("Eracle"))
        assert body.rstrip().splitlines()[-1] == ATTRIBUTION

    def test_attribution_follows_the_signature(self):
        body = self._sent_body(self._box("Eracle"))
        assert body.index("Eracle") < body.index(ATTRIBUTION)

    def test_follow_up_also_carries_attribution(self):
        """Threaded replies go through the same assembly, so they carry it too."""
        body = self._sent_body(self._box("Eracle"), in_reply_to="<prior@corp.com>")
        assert body.endswith(f"{ATTRIBUTION}\n")

    def test_body_is_not_logged_on_send(self, caplog):
        with caplog.at_level("INFO", logger="cold_outreach.emails.sender"):
            self._sent_body(self._box("Eracle"))
        records = [r for r in caplog.records if r.name == "cold_outreach.emails.sender"]
        assert len(records) == 1
        logged = records[0].getMessage()
        assert "Body" not in logged and ATTRIBUTION not in logged
        assert "lead@corp.com" in logged and "Hi" in logged


# ── send_first_email (the step) ───────────────────────────────────


@pytest.mark.django_db
class TestSendFirstEmail:
    def _run(self, deal, box, subject="Hi there", message="Short opener."):
        with patch(
            "openoutreach.core.db.summaries.materialize_profile_summary_if_missing",
        ), patch(
            "cold_outreach.core.agents.outreach.run_outreach_agent",
            return_value=OutreachDecision(
                action="send_message", subject=subject, message=message),
        ), patch(
            "cold_outreach.emails.sender.send_email",
            side_effect=lambda mailbox, *a, **kw: maillog.outbound(
                mailbox, message_id="mid@corp.com"),
        ) as send:
            next_state = send_first_email(deal, box)
        deal.save()
        return send, next_state

    def test_sends_records_and_moves_to_emailed(self, campaign, operator):
        box = _box(daily_limit=10)
        deal = _ready(campaign, "lead@corp.com")

        send, next_state = self._run(deal, box)

        # The operator's own campaign → they get a BCC of their own outreach, and
        # the campaign rides along so the sender can log the body it sent.
        send.assert_called_once_with(
            box, "lead@corp.com", "Hi there", "Short opener.",
            campaign=campaign,
            bcc="testuser@example.com",
        )
        assert next_state == DealState.EMAILED
        deal.refresh_from_db()
        assert deal.mailbox == box
        assert deal.email_subject == "Hi there"
        assert deal.thread is not None
        assert deal.email_sent_at is not None

    def test_the_deal_points_at_the_thread_the_send_opened(self, campaign, operator):
        """One record, not two: the deal borrows the log's thread rather than
        keeping its own copy of the conversation to drift from it."""
        box = _box(daily_limit=10)
        deal = _ready(campaign, "lead@corp.com")
        self._run(deal, box)

        deal.refresh_from_db()
        message = deal.thread.messages.get()
        assert message.is_outbound
        assert message.message_id == "mid@corp.com"

    def test_no_follow_up_clock_is_armed(self, campaign, operator):
        """Nobody is chased, so a sent deal carries no schedule at all."""
        box = _box(daily_limit=10)
        deal = _ready(campaign, "lead@corp.com")
        self._run(deal, box)

        deal.refresh_from_db()
        assert deal.not_before is None

    def test_the_box_is_spaced_out_afterwards(self, campaign, operator):
        box = _box(daily_limit=10)
        self._run(_ready(campaign, "lead@corp.com"), box)

        box.refresh_from_db()
        assert box.next_send_at > timezone.now()
        assert Mailbox.objects.free_for_first_email() is None

    def test_the_gap_lands_in_the_jittered_band(self, campaign, operator):
        """3 min + U[30s, 90s] — never the floor exactly, never past 4.5 minutes."""
        from datetime import timedelta

        box = _box(daily_limit=10)
        deal = _ready(campaign, "lead@corp.com")
        self._run(deal, box)

        box.refresh_from_db()
        deal.refresh_from_db()
        gap = box.next_send_at - deal.email_sent_at
        assert timedelta(seconds=210) <= gap <= timedelta(seconds=270)

    def test_no_bcc_on_a_freemium_campaign(self, campaign, operator):
        """Freemium outreach is OpenOutreach's own — the operator gets no copy."""
        campaign.is_freemium = True
        campaign.save(update_fields=["is_freemium"])
        box = _box(daily_limit=10)

        send, _ = self._run(_ready(campaign, "lead@corp.com"), box)

        assert send.call_args.kwargs["bcc"] is None

    def test_a_lead_suppressed_mid_run_is_not_emailed(self, campaign, operator):
        """An unsubscribe can land in the seconds the agent takes to write."""
        box = _box(daily_limit=10)
        deal = _ready(campaign, "lead@corp.com")

        with patch(
            "openoutreach.core.db.summaries.materialize_profile_summary_if_missing",
        ), patch(
            "cold_outreach.core.agents.outreach.run_outreach_agent",
            side_effect=lambda d: (
                type(d.lead).objects.filter(pk=d.lead.pk).update(disqualified=True)
                or OutreachDecision(action="send_message", subject="s", message="m")
            ),
        ), patch(
            "cold_outreach.emails.sender.send_email",
        ) as send:
            next_state = send_first_email(deal, box)

        send.assert_not_called()
        assert next_state is None
