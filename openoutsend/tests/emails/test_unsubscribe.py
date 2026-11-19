# openoutsend/tests/emails/test_unsubscribe.py
"""Opt-out: the advertised mechanism, the three detection paths, and the enforcement.

The three legs, and what each one locks down:

  * **Advertised** — every send carries the ``List-Unsubscribe`` header *and* a
    visible reply-line. The header is what receiving filters read; the line
    reaches the clients that render no unsubscribe button of their own. Both are
    asserted on the same message, since either alone leaves someone with no exit
    but the spam button.
  * **Detected** — a client-generated unsubscribe (no threading headers, found
    box-wide by the ``+unsub`` alias during the mail pass), a reply carrying the
    word the reply-line asks for (matched by code in the same pass, on whichever
    step it answers), and any other wording of the wish (read by the model in the
    deal's conversation) all reach the same suppression.
  * **Enforced** — the address is on the list before anything is written, and the
    send path asks the list again after the agent has written.

What the list itself does once an address reaches it — which deals close, what a
second suppression changes — is ``tests/leads/test_suppression.py``.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from pydantic_ai.models.test import TestModel

from openoutsend.core.agents.outreach import Email
from openoutsend.core.sequences import cold, key
from openoutsend.emails.mail_pass import run_mail_pass
from openoutsend.emails.models import Mailbox, Thread
from openoutsend.emails.sender import (
    ATTRIBUTION,
    OPT_OUT_LINE,
    OPT_OUT_WORD,
    send_email,
    suppressed,
    unsubscribe_address,
)
from openoutsend.emails.steps.send import Plan, send_step
from openoutsend.errors import OutsendError
from openoutsend.leads.lifecycle import machine_of
from openoutsend.leads.models import Outcome, Suppression
from openoutsend.leads.states import COMPLETED, CONVERSING, awaiting, machine_name, sending
from openoutsend.leads.suppression import suppress_email
from openoutsend.tests.emails import maillog
from openoutsend.tests.emails.fake_imap import RECEIVED_AT, FakeIMAP, message
from openoutsend.tests.factories import DealFactory, LeadFactory

SENDER = "s@infra.com"
ALIAS = "s+unsub@infra.com"
ROOT = "root@infra.com"


def _box(**kwargs) -> Mailbox:
    return maillog.mailbox(SENDER, daily_limit=10, **kwargs)


def _sent_message(**kwargs):
    """The assembled EmailMessage for one send, without touching SMTP."""
    box = maillog.mailbox(SENDER, signature="Eracle")
    with patch("openoutsend.emails.sender._deliver") as deliver:
        send_email(box, "lead@corp.com", "Hi", "Body", **kwargs)
    return deliver.call_args.args[1]


# ── The advertised mechanism ──────────────────────────────────────


@pytest.mark.django_db
class TestOptOutIsAdvertised:
    def test_opener_carries_the_list_unsubscribe_header(self):
        assert _sent_message()["List-Unsubscribe"] == f"<mailto:{ALIAS}?subject=unsubscribe>"

    def test_a_later_email_in_the_thread_carries_it_too(self):
        """Threaded sends go through the same assembly, so they carry it too."""
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

    def test_the_line_quotes_the_word_the_mail_pass_matches(self):
        """Reword the sentence freely; the word it asks for is the one code honours."""
        assert f'"{OPT_OUT_WORD}"' in OPT_OUT_LINE

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
    with patch("openoutsend.emails.sync._connect", return_value=fake):
        run_mail_pass()
    return Suppression.objects.count() - before


@pytest.mark.django_db
class TestAliasOptOut:
    """A client's unsubscribe button mints a fresh message with no threading
    headers at all, so the thread reader can never see it. The alias is the
    whole signal."""

    def test_an_alias_message_suppresses_its_sender(self):
        lead = LeadFactory(email="p@corp.com")
        deal = DealFactory(lead=lead, state=CONVERSING)

        assert _read(_box(), FakeIMAP([message(7, to=ALIAS, sender="p@corp.com")])) == 1

        deal.refresh_from_db()
        assert suppressed(lead)
        assert (deal.state, deal.outcome) == (COMPLETED, Outcome.UNSUBSCRIBED)

    def test_ordinary_inbox_mail_is_left_alone(self):
        lead = LeadFactory(email="p@corp.com")
        assert _read(_box(), FakeIMAP([message(7, to=SENDER, sender="p@corp.com")])) == 0
        assert not suppressed(lead)

    def test_a_display_name_around_the_alias_still_matches(self):
        LeadFactory(email="p@corp.com")
        to = f'"Unsubscribe" <{ALIAS}>'
        assert _read(_box(), FakeIMAP([message(7, to=to, sender="P@corp.com")])) == 1

    def test_the_opt_out_is_recorded_as_a_message_of_its_own(self):
        """It is a fact about the box before it is a decision about a person."""
        from openoutsend.emails.models import Kind, Message

        LeadFactory(email="p@corp.com")
        _read(_box(), FakeIMAP([message(7, to=ALIAS, sender="p@corp.com")]))

        row = Message.objects.get(direction="in")
        assert row.kind == Kind.OPT_OUT
        assert row.processed_at is not None

    def test_rereading_the_same_message_changes_nothing(self):
        """Re-reading a box must be free — the log is keyed on the Message-ID."""
        lead = LeadFactory(email="p@corp.com")
        deal = DealFactory(lead=lead, state=CONVERSING)
        box = _box()
        fake = FakeIMAP([message(7, to=ALIAS, sender="p@corp.com")])

        assert _read(box, fake) == 1
        coverage = box.coverage.get()
        coverage.last_uid = 0            # as a UIDVALIDITY change would leave it
        coverage.save(update_fields=["last_uid"])
        assert _read(box, fake) == 0     # already stored, already honoured

        deal.refresh_from_db()
        assert (deal.state, deal.outcome) == (COMPLETED, Outcome.UNSUBSCRIBED)
        assert Suppression.objects.count() == 1

    def test_an_unreachable_box_keeps_its_coverage(self):
        """A network fault is not evidence that there was no mail to read."""
        box = _box()
        _read(box, FakeIMAP([message(7, to=SENDER, sender="x@corp.com")]))

        fake = FakeIMAP([])
        fake.login = MagicMock(side_effect=OSError("connection reset"))
        assert _read(box, fake) == 0
        assert box.coverage.get().last_uid == 7


# ── Detection: the footer's word (the mail pass) ──────────────────


def _written_to(box, *, state=CONVERSING, sequence="cold", email="p@corp.com"):
    """A deal resting in *state* whose lead we wrote to, its thread kept against it.

    Sent the day before the fake inbox's mail, so a reply read from it is the thread's
    newest turn.
    """
    sent = maillog.outbound(box, to=email, message_id=ROOT, subject="Hi",
                            sent_at=RECEIVED_AT - timedelta(days=1))
    deal = DealFactory(
        lead=LeadFactory(email=email),
        sequence=sequence,
        state=state,
        mailbox=box,
        email_subject="Hi",
        thread=sent.thread,
    )
    Thread.objects.filter(pk=sent.thread_id).update(
        machine=machine_name(deal.sequence), entity=str(deal.pk))
    return deal


def _reply(body, uid=7):
    """Their reply to our email, as the inbox holds it."""
    return message(uid, to=SENDER, sender="p@corp.com", references=f"<{ROOT}>", body=body)


@pytest.fixture
def three_steps(tmp_path):
    """A sequence of three emails, so a deal can be answered between its steps."""
    folder = tmp_path / "launch"
    folder.mkdir()
    (folder / "E0-first.email.j2").write_text("Subject: Hi\n\nFirst.\n")
    (folder / "E1-2d-second.email.j2").write_text("Second.\n")
    (folder / "E2-3d-third.email.j2").write_text("Third.\n")
    return key(str(folder))


def _model_turn(answer=None):
    """The conversation's model call, stood in for so a test can see whether it was made.

    *answer* takes the turn, given the entity and the turn's schema; left out, the
    conversation ends ``not_interested``.
    """
    def end_it(entity, schema):
        return schema(to=COMPLETED, outcome="not_interested")

    answer = answer or end_it
    return patch("openoutsend.machines.steps.ModelTurn.ask",
                 side_effect=lambda prompt, entity, schema: answer(entity, schema))


@pytest.mark.django_db
class TestTheOptOutWord:
    """The reply-line asks for one word, and code honours it before any model is consulted."""

    @pytest.mark.parametrize("body", ["stop", "Stop", "STOP", "please stop emailing me"])
    def test_a_reply_carrying_the_word_suppresses_its_sender(self, body):
        box = _box()
        deal = _written_to(box)

        assert _read(box, FakeIMAP([_reply(body)])) == 1

        deal.refresh_from_db()
        assert suppressed(deal.lead)
        assert (deal.state, deal.outcome) == (COMPLETED, Outcome.UNSUBSCRIBED)

    @pytest.mark.parametrize("state", [awaiting(1), sending(1), awaiting(2), sending(2), CONVERSING])
    def test_it_is_honoured_on_whichever_step_it_answers(self, three_steps, state):
        """A reply never stops a step, so this is the only thing that ends a deal mid-sequence."""
        box = _box()
        deal = _written_to(box, sequence=three_steps, state=state)

        assert _read(box, FakeIMAP([_reply("Stop")])) == 1

        deal.refresh_from_db()
        assert (deal.state, deal.outcome) == (COMPLETED, Outcome.UNSUBSCRIBED)

    def test_no_model_is_asked_and_nothing_is_sent(self, site_config):
        """The whole pass, with the deal in its conversation: the reply is never a turn."""
        from openoutsend.send_pass import run_send_pass

        box = _box()
        deal = _written_to(box)

        with patch("openoutsend.emails.sync._connect", return_value=FakeIMAP([_reply("Stop")])), \
             patch("openoutsend.core.agents.outreach.write_email") as write, \
             _model_turn() as turn, \
             patch("openoutsend.emails.sender._deliver") as deliver:
            result = run_send_pass(narrate=False)

        turn.assert_not_called()
        write.assert_not_called()
        deliver.assert_not_called()
        assert (result.answered, result.ended, result.failed) == (0, 0, 0)
        deal.refresh_from_db()
        assert (deal.state, deal.outcome) == (COMPLETED, Outcome.UNSUBSCRIBED)

    def test_its_letters_inside_another_word_reach_the_conversation_instead(self, site_config):
        from openoutsend.send_pass import run_send_pass

        box = _box()
        deal = _written_to(box)

        with patch("openoutsend.emails.sync._connect",
                   return_value=FakeIMAP([_reply("We have been nonstopping since March.")])), \
             model_saying(), _model_turn() as turn, \
             patch("openoutsend.emails.sender._deliver"):
            run_send_pass(narrate=False)

        turn.assert_called_once()
        deal.refresh_from_db()
        assert not suppressed(deal.lead)
        assert (deal.state, deal.outcome) == (COMPLETED, Outcome.NOT_INTERESTED)

    def test_an_out_of_office_carrying_the_word_suppresses_nobody(self):
        box = _box()
        deal = _written_to(box)
        away = message(7, to=SENDER, sender="p@corp.com", subject="Automatic reply: Hi",
                       in_reply_to=f"<{ROOT}>", body="I am away. Please stop by on Monday.",
                       headers=("Auto-Submitted: auto-replied",))

        assert _read(box, FakeIMAP([away])) == 0

        assert not suppressed(deal.lead)
        assert deal.state == CONVERSING

    def test_a_bounce_carrying_the_word_suppresses_nobody(self):
        box = _box()
        deal = _written_to(box)
        report = message(7, to=SENDER, sender="mailer-daemon@googlemail.com",
                         subject="Delivery Status Notification (Delay)",
                         message_id="<ndr7@googlemail.com>", in_reply_to=f"<{ROOT}>",
                         body="Delivery is delayed. We will stop trying in 3 days.\r\n")

        assert _read(box, FakeIMAP([report])) == 0

        assert not suppressed(deal.lead)
        assert deal.state == CONVERSING


# ── Detection: the worded unsubscribe (the conversation) ──────────


def _in_conversation(email="p@corp.com", body="Please take me off your list."):
    """A deal in its conversation with an unanswered reply — what a turn picks up."""
    deal = _written_to(_box(), email=email)
    maillog.inbound(deal.mailbox, thread=deal.thread, sender=email, body=body)
    return deal


def model_saying(**fields):
    """The install's model, taking the turn with *fields* (as ``tests/test_machines.py`` does)."""
    return patch("openoutsend.core.llm.get_llm_model",
                 return_value=TestModel(custom_output_args=fields))


def _take_turn(deal):
    return machine_of(deal).fire(deal.pk, entity=deal)


@pytest.mark.django_db
class TestWordedUnsubscribe:
    """Any wording but the footer's own is read by the model, once the deal converses."""

    def test_a_turn_ending_unsubscribed_lists_the_address_and_closes_the_deal(self, site_config):
        deal = _in_conversation()
        elsewhere = DealFactory(lead=LeadFactory(email="P@Corp.com"))   # same person, another deal

        with model_saying(to=COMPLETED, outcome="unsubscribed"), \
             patch("openoutsend.emails.sender.send_email") as send:
            move = _take_turn(deal)

        assert (move.decided_by, move.to_state) == ("llm:test:test", COMPLETED)
        deal.refresh_from_db()
        assert (deal.state, deal.outcome) == (COMPLETED, Outcome.UNSUBSCRIBED)
        assert suppressed(deal.lead)
        assert Suppression.objects.get().reason == "worded unsubscribe in a reply"
        # The request is honoured silently, and in every deal that would have emailed them.
        send.assert_not_called()
        elsewhere.refresh_from_db()
        assert (elsewhere.state, elsewhere.outcome) == (COMPLETED, Outcome.UNSUBSCRIBED)

    def test_the_deal_is_ended_once_by_the_turn_itself(self, site_config):
        """The suppression ends the lead's *other* deals; this one leaves by its own move."""
        deal = _in_conversation()

        with model_saying(to=COMPLETED, outcome="unsubscribed"):
            _take_turn(deal)

        moves = machine_of(deal).history(deal.pk)
        assert [move.decided_by for move in moves if move.to_state == COMPLETED] == ["llm:test:test"]

    def test_an_ordinary_ending_still_completes(self, site_config):
        """The reading above must not swallow the other seven outcomes."""
        deal = _in_conversation(body="Not for us, thanks.")

        with model_saying(to=COMPLETED, outcome="not_interested"):
            assert _take_turn(deal).to_state == COMPLETED

        deal.refresh_from_db()
        assert deal.outcome == Outcome.NOT_INTERESTED
        assert not suppressed(deal.lead)

    def test_the_deal_closes_even_when_the_address_matches_nothing(self, site_config):
        """``suppress_email`` is keyed on the address, the move on the deal. A lead
        with no address would otherwise stay in its conversation with an unanswered
        reply — permanently actionable, re-decided every pass."""
        deal = _in_conversation()
        deal.lead.email = ""
        deal.lead.save()

        with model_saying(to=COMPLETED, outcome="unsubscribed"):
            assert _take_turn(deal).to_state == COMPLETED

        deal.refresh_from_db()
        assert (deal.state, deal.outcome) == (COMPLETED, Outcome.UNSUBSCRIBED)


# ── Enforcement at the send call sites ───────────────────────────


@pytest.mark.django_db
class TestSendGuards:
    def test_suppressed_reads_the_list_not_the_in_memory_copy(self):
        lead = LeadFactory(email="p@corp.com")
        suppress_email("p@corp.com")
        assert suppressed(lead)  # the copy selected before the opt-out landed

    def test_a_first_email_is_not_sent_to_a_lead_suppressed_mid_run(self):
        """The agent runs for seconds — the query that selected this deal is
        already out of date by the time there is a message to send."""
        box = _box()
        deal = DealFactory(
            lead=LeadFactory(email="p@corp.com"),
            state=sending(0),
        )

        def _suppress_then_write(target, prompt, opener=False):
            suppress_email(target.lead.email)
            return Email(subject="Hi", message="Body")

        with patch("openoutsend.core.agents.outreach.write_email",
                   side_effect=_suppress_then_write), \
             patch("openoutsend.leads.summaries.materialize_profile_summary_if_missing"), \
             patch("openoutsend.emails.sender.send_email") as send:
            assert send_step(deal, cold().first, Plan(box)) is False

        send.assert_not_called()
        deal.refresh_from_db()
        assert (deal.state, deal.outcome) == (COMPLETED, Outcome.UNSUBSCRIBED)

    def test_an_answer_is_not_sent_to_a_lead_suppressed_mid_run(self, site_config):
        deal = _in_conversation(body="Tell me more.")

        def _suppress_then_answer(entity, schema):
            suppress_email(entity.lead.email)
            return schema(to=CONVERSING, message="Body")

        with model_saying(), _model_turn(_suppress_then_answer), \
             patch("openoutsend.emails.sender.send_email") as send, \
             pytest.raises(OutsendError, match="on the suppression list"):
            _take_turn(deal)

        # The turn is refused rather than moved: a pass counts it as failed, and the
        # suppression has already ended the deal on its way onto the list.
        send.assert_not_called()
