# openoutsend/tests/emails/test_send.py
"""The first-email path: the per-box cap and spacing, the ready pool, and the step.

A first email is the only cold volume this sender produces, so it is the only send
under a cap — replies are exempt (see ``test_reply.py``)."""
import logging

import pytest
from unittest.mock import patch

from django.utils import timezone

from openoutsend.core.agents.outreach import OutreachDecision
from openoutsend.core.sequences import cold
from openoutsend.emails.models import Mailbox, Thread
from openoutsend.emails.sender import (
    ATTRIBUTION,
    OPT_OUT_LINE,
    operator_bcc,
    send_email,
)
from openoutsend.emails.steps.send import send_first_email, step_fields
from openoutsend.leads.models import DealState
from openoutsend.planner import emailable_deals, free_for_first_email
from openoutsend.tests.emails import maillog
from openoutsend.tests.factories import DealFactory, LeadFactory


def _box(email="a@b.com", daily_limit=10):
    return maillog.mailbox(email, daily_limit=daily_limit)


def _ready(email="lead@corp.com"):
    """A deal waiting for its first email — ingested, with an address."""
    return DealFactory(
        lead=LeadFactory(email=email),
        state=DealState.READY,
    )


def _record_send(deal, box, when=None):
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


def _record_reply_exchange(deal, box, when):
    """One inbound reply and our answer, both inside an existing thread."""
    maillog.inbound(box, thread=deal.thread, sent_at=when)
    maillog.outbound(box, thread=deal.thread, to=deal.lead.email, sent_at=when)


# ── Mailbox pacing ────────────────────────────────────────────────


@pytest.mark.django_db
class TestMailboxCap:
    """The cap counts *people first contacted today*, not messages sent today."""

    def test_a_first_email_spends_one(self):
        box = _box(daily_limit=10)
        assert box.sent_today() == 0
        _record_send(_ready(), box)
        assert box.sent_today() == 1
        assert box.headroom_today() == 9

    def test_replies_inside_an_older_thread_are_free(self):
        """Answering someone who wrote back is not cold volume, so it costs no cap."""
        from datetime import timedelta

        box = _box(daily_limit=10)
        yesterday = timezone.now() - timedelta(days=1)
        deal = _record_send(_ready(), box, when=yesterday)
        _record_reply_exchange(deal, box, when=timezone.now())

        assert box.sent_today() == 0
        assert box.headroom_today() == 10

    def test_remaining_today_sums_headroom_across_boxes(self):
        _box("a@b.com", daily_limit=3)
        _box("c@d.com", daily_limit=5)
        assert Mailbox.objects.remaining_today() == 8

    def test_remaining_today_zero_with_no_boxes(self):
        assert Mailbox.objects.remaining_today() == 0


@pytest.mark.django_db
class TestPickingABox:
    def test_picks_the_box_with_most_headroom(self):
        light = _box("light@b.com", daily_limit=10)
        heavy = _box("heavy@b.com", daily_limit=10)
        for _ in range(4):
            _record_send(_ready(), heavy)
        with patch("openoutsend.core.sending_window.within_sending_window", return_value=True):
            assert free_for_first_email() == light

    def test_none_when_every_box_is_capped(self):
        box = _box(daily_limit=1)
        _record_send(_ready(), box)
        assert free_for_first_email() is None

    def test_a_box_still_spacing_out_is_not_free(self):
        """The 3-minute floor between two cold emails, per box."""
        from datetime import timedelta

        box = _box(daily_limit=10)
        box.next_send_at = timezone.now() + timedelta(minutes=3)
        box.save(update_fields=["next_send_at"])
        assert free_for_first_email() is None

    def test_a_box_past_its_spacing_is_free_again(self):
        from datetime import timedelta

        box = _box(daily_limit=10)
        box.next_send_at = timezone.now() - timedelta(seconds=1)
        box.save(update_fields=["next_send_at"])
        with patch("openoutsend.core.sending_window.within_sending_window", return_value=True):
            assert free_for_first_email() == box

    def test_the_daily_ledger_starts_at_the_operators_midnight(self):
        """Not the server's UTC midnight, which for a US operator falls at 19:00 —
        inside the window, so the cap would reset an hour before the day ends."""
        from zoneinfo import ZoneInfo

        from openoutsend.emails.models.mailbox import _local_midnight

        zone = ZoneInfo("America/New_York")
        with patch("openoutsend.emails.models.mailbox.operator_timezone", return_value=zone):
            midnight = _local_midnight()
        assert midnight.astimezone(zone).hour == 0

    def test_no_box_is_free_outside_the_sending_window(self):
        """Out of hours nothing opens a conversation, however much headroom is left."""
        _box(daily_limit=10)
        with patch("openoutsend.core.sending_window.within_sending_window", return_value=False):
            assert free_for_first_email() is None


# ── Email pool ────────────────────────────────────────────────────


@pytest.mark.django_db
class TestEmailableDeals:
    def test_returns_only_deals_waiting_for_a_first_email(self):
        ready = _ready()
        DealFactory(lead=LeadFactory(), state=DealState.EMAILED)
        DealFactory(lead=LeadFactory(), state=DealState.COMPLETED)
        assert list(emailable_deals()) == [ready]

    def test_excludes_a_lead_with_no_address(self):
        """A row can arrive without one — an address is an enrichment, not a promise."""
        _ready(email="")
        assert list(emailable_deals()) == []

    def test_oldest_first(self):
        first = _ready("first@c.com")
        second = _ready("second@c.com")
        assert list(emailable_deals()) == [first, second]


@pytest.mark.django_db
class TestSendEmailBcc:
    def test_bcc_header_set_when_address_given(self):
        box = maillog.mailbox("s@infra.com")
        with patch("openoutsend.emails.sender._deliver") as deliver:
            send_email(box, "lead@corp.com", "Hi", "Body", bcc="me@mine.com")
        message = deliver.call_args.args[1]
        assert message["Bcc"] == "me@mine.com"

    def test_no_bcc_header_when_address_blank(self):
        box = maillog.mailbox("s@infra.com")
        with patch("openoutsend.emails.sender._deliver") as deliver:
            send_email(box, "lead@corp.com", "Hi", "Body", bcc="")
        message = deliver.call_args.args[1]
        assert message["Bcc"] is None


@pytest.mark.django_db
class TestSentBodyLogging:
    """What the agent wrote is logged; the boilerplate appended to it is not.

    Every send is the operator's own — it is their outreach, from their box, and
    they receive each of these in full by BCC — so the log discloses nothing they do
    not already hold.
    """

    def _send(self, caplog):
        box = maillog.mailbox("s@infra.com", signature="— Ercole")
        with caplog.at_level(logging.INFO, logger="openoutsend.emails.sender"), \
             patch("openoutsend.emails.sender._deliver"):
            send_email(box, "lead@corp.com", "Hi there", "How do you do discovery today?")
        return caplog.text

    def test_logs_what_the_agent_generated(self, caplog):
        text = self._send(caplog)
        assert "Subject: Hi there" in text
        assert "How do you do discovery today?" in text

    def test_the_appended_boilerplate_is_not_logged(self, caplog):
        """Signature, opt-out and attribution are the same on every send — noise here."""
        text = self._send(caplog)
        assert "— Ercole" not in text
        assert ATTRIBUTION.strip() not in text
        assert OPT_OUT_LINE.strip() not in text


@pytest.mark.django_db
class TestOperatorBcc:
    def test_the_operator_is_copied_on_their_own_outreach(self, operator):
        assert operator_bcc(operator) == operator.email

    def test_blank_operator_email_yields_no_bcc(self, operator):
        """An empty address would set an empty Bcc header, not "no copy"."""
        operator.email = ""
        assert operator_bcc(operator) is None


@pytest.mark.django_db
class TestSendEmailSignature:
    def _sent_body(self, signature: str | None) -> str:
        box = maillog.mailbox("s@infra.com", signature=signature)
        with patch("openoutsend.emails.sender._deliver") as deliver:
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
        with patch("openoutsend.emails.sender._deliver") as deliver:
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

    def test_the_metadata_line_names_the_recipient_and_subject(self, caplog):
        """Two records per send: what went where, then what was written (see above)."""
        with caplog.at_level("INFO", logger="openoutsend.emails.sender"):
            self._sent_body(self._box("Eracle"))
        metadata = [r for r in caplog.records
                    if r.name == "openoutsend.emails.sender"][0].getMessage()
        assert "lead@corp.com" in metadata and "Hi" in metadata
        assert ATTRIBUTION not in metadata


# ── The prompt line rides on the send ─────────────────────────────


@pytest.mark.django_db
class TestPromptLineIsRecorded:
    """What makes comparing prompt lines later a query rather than an experiment.

    None of this is read yet. It is here now because it can only be collected from the
    first send onward — a column added after a thousand sends leaves a thousand rows
    that can never say which line wrote them.
    """

    def test_an_opener_records_the_line_that_wrote_it(self):
        from openoutsend.core.prompt_lines import choose
        from openoutsend.emails.sender import send_email

        line = choose("plain-ask")
        box = maillog.mailbox("s@infra.com")

        with patch("openoutsend.emails.sender._deliver"):
            row = send_email(box, "p@corp.com", "Hi", "Body", prompt_line=line)

        assert row.prompt_line_id == "plain-ask"
        assert row.prompt_line_digest == line.digest

    def test_a_reply_records_no_line(self):
        """A reply answers what they wrote; attributing it would be noise in the log."""
        from openoutsend.emails.sender import send_email

        box = maillog.mailbox("s@infra.com")

        with patch("openoutsend.emails.sender._deliver"):
            row = send_email(box, "p@corp.com", "Re: Hi", "Body", prompt_line=None)

        assert row.prompt_line_id == ""
        assert row.prompt_line_digest == ""


# ── send_first_email (the step) ───────────────────────────────────


@pytest.mark.django_db
class TestSendFirstEmail:
    def _run(self, deal, box, subject="Hi there", message="Short opener."):
        with patch(
            "openoutsend.leads.summaries.materialize_profile_summary_if_missing",
        ), patch(
            "openoutsend.core.agents.outreach.run_outreach_agent",
            return_value=OutreachDecision(
                action="send_message", subject=subject, message=message),
        ), patch(
            "openoutsend.emails.sender.send_email",
            side_effect=lambda mailbox, *a, **kw: maillog.outbound(
                mailbox, message_id="mid@corp.com"),
        ) as send:
            next_state = send_first_email(deal, box, cold().first, None)
        deal.save()
        return send, next_state

    def test_sends_records_and_moves_to_emailed(self, operator):
        box = _box(daily_limit=10)
        deal = _ready("lead@corp.com")

        send, next_state = self._run(deal, box)

        # Every send is the operator's own, so they get a BCC of their outreach.
        send.assert_called_once()
        assert send.call_args.args == (box, "lead@corp.com", "Hi there", "Short opener.")
        kwargs = send.call_args.kwargs
        assert kwargs["bcc"] == operator.email
        assert kwargs["prompt_line"] is None
        assert next_state == DealState.EMAILED
        deal.refresh_from_db()
        assert deal.mailbox == box
        assert deal.email_subject == "Hi there"
        assert deal.thread is not None
        assert deal.email_sent_at is not None

    def test_it_is_written_from_cold_e0_and_records_the_step(self, operator):
        """The rendered prompt goes on the send, so the next message's history replays it."""
        box = _box(daily_limit=10)
        deal = _ready("lead@corp.com")

        send, _ = self._run(deal, box)

        kwargs = send.call_args.kwargs
        assert (kwargs["step"].number, kwargs["step"].path.name) == (0, "E0-opener.prompt.j2")
        assert "Write a first email to this lead." in kwargs["prompt"]
        deal.refresh_from_db()
        assert deal.step == 0

    def test_the_deal_points_at_the_thread_the_send_opened(self, operator):
        """One record, not two: the deal borrows the log's thread rather than
        keeping its own copy of the conversation to drift from it."""
        box = _box(daily_limit=10)
        deal = _ready("lead@corp.com")
        self._run(deal, box)

        deal.refresh_from_db()
        message = deal.thread.messages.get()
        assert message.is_outbound
        assert message.message_id == "mid@corp.com"

    def test_the_box_is_spaced_out_afterwards(self, operator):
        box = _box(daily_limit=10)
        self._run(_ready("lead@corp.com"), box)

        box.refresh_from_db()
        assert box.next_send_at > timezone.now()
        assert free_for_first_email() is None

    def test_the_gap_lands_in_the_jittered_band(self, operator):
        """3 min + U[30s, 90s] — never the floor exactly, never past 4.5 minutes."""
        from datetime import timedelta

        box = _box(daily_limit=10)
        deal = _ready("lead@corp.com")
        self._run(deal, box)

        box.refresh_from_db()
        deal.refresh_from_db()
        gap = box.next_send_at - deal.email_sent_at
        assert timedelta(seconds=210) <= gap <= timedelta(seconds=270)

    def test_a_lead_suppressed_mid_run_is_not_emailed(self, operator):
        """An unsubscribe can land in the seconds the agent takes to write."""
        from openoutsend.leads.suppression import suppress_email

        box = _box(daily_limit=10)
        deal = _ready("lead@corp.com")

        def write_while_they_opt_out(d, prompt, opener=False):
            suppress_email(d.lead.email, reason="opted out mid-run")
            return OutreachDecision(action="send_message", subject="s", message="m")

        with patch(
            "openoutsend.leads.summaries.materialize_profile_summary_if_missing",
        ), patch(
            "openoutsend.core.agents.outreach.run_outreach_agent",
            side_effect=write_while_they_opt_out,
        ), patch(
            "openoutsend.emails.sender.send_email",
        ) as send:
            next_state = send_first_email(deal, box, cold().first, None)

        send.assert_not_called()
        assert next_state is None


@pytest.mark.django_db
class TestStepFields:
    """What a step prompt's ``{{ field }}`` can name."""

    def test_the_piped_fields_sit_beside_the_lead_columns(self):
        deal = DealFactory(
            lead=LeadFactory(first_name="Asha"),
            fields={"order_url": "https://pay.example/1", "leads": [{"name": "Bo"}]},
        )

        fields = step_fields(deal, None)

        assert fields["first_name"] == "Asha"
        assert fields["order_url"] == "https://pay.example/1"
        assert fields["leads"] == [{"name": "Bo"}]
        assert fields["prompt_line"] == ""

    def test_a_piped_field_cannot_shadow_the_drawn_prompt_line(self):
        from openoutsend.core.prompt_lines import choose

        line = choose("plain-ask")
        deal = DealFactory(fields={"prompt_line": "piped"})

        assert step_fields(deal, line)["prompt_line"] == line.prompt
