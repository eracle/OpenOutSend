# openoutsend/tests/test_agent_draft.py
"""``send --agent-draft`` — the calling agent writes the replies, openers and later
steps instead of AI_MODEL: one draft at a time, a reply ahead of a step, and an answer
kept until a box is free to send it.

Covers the pipeline layer only (``_send_agent_draft``, ``_answer_drafted_reply``); the
CLI's own flag parsing is covered by ``tests/test_cli.py``.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from openoutsend.core.agent_draft import AgentDraft
from openoutsend.core.agents.outreach import Email
from openoutsend.core.sequences import key
from openoutsend.emails.models import Direction, Message, Thread
from openoutsend.errors import DraftPending, OutsendError
from openoutsend.leads.lifecycle import machine_of
from openoutsend.leads.models import Outcome, PendingDraft
from openoutsend.leads.states import COMPLETED, CONVERSING, awaiting, machine_name, sending
from openoutsend.send_pass import run_send_pass
from openoutsend.tests.emails import maillog
from openoutsend.tests.factories import DealFactory, LeadFactory, UserFactory, put_in_state

pytestmark = pytest.mark.django_db


def _waiting(address="lead@acme.com"):
    """A cold deal waiting for its opener, which is due the moment it arrives."""
    return DealFactory(lead=LeadFactory(email=address, profile_text="cto at acme"))


def _in_thread(deal, box) -> Thread:
    """Give *deal* the thread its opener started from *box*, kept against the deal."""
    deal.thread = Thread.objects.create(mailbox=box, machine=machine_name(deal.sequence),
                                        entity=str(deal.pk))
    deal.save()
    return deal.thread


def _past_its_opener(tmp_path, box, **kwargs):
    """A deal in a two-step sequence whose opener went out long enough ago that ``E1``
    is due: its wait has run out, and the pass makes it due."""
    (tmp_path / "E0-open.prompt.j2").write_text("Open to {{ first_name }}.")
    (tmp_path / "E1-2d-steer.prompt.j2").write_text("Steer the list for {{ product }}.")
    deal = DealFactory(
        lead=LeadFactory(email="lead@acme.com", first_name="Ada"),
        mailbox=box, sequence=key(str(tmp_path)), fields={"product": "portraits"},
        email_subject="Which of the ten was wrong?", **kwargs,
    )
    thread = _in_thread(deal, box)
    put_in_state(deal, awaiting(1), at=timezone.now() - timedelta(days=10))
    maillog.outbound(box, thread=thread, step=0, message_id="opener@openoutreach.app",
                     subject=deal.email_subject,
                     sent_at=timezone.now() - timedelta(days=10))
    return deal


@pytest.fixture
def no_mail():
    """Neither the read nor the warmth measurement matter to this step."""
    with patch("openoutsend.emails.mail_pass.run_mail_pass", return_value=(0, 0, 0)), \
            patch("openoutsend.emails.warmth.measure_pool"):
        yield


@pytest.fixture
def no_model():
    """Neither model — the one that writes a step, the one that takes a turn — may be asked."""
    with patch("openoutsend.core.agents.outreach.write_email") as writes, \
            patch("openoutsend.machines.handlers.ModelTurn.ask") as turns:
        yield
    writes.assert_not_called()
    turns.assert_not_called()


class TestStoppingForAnOpener:
    def test_a_fresh_deal_is_handed_back_instead_of_asking_the_llm(self, no_mail, no_model):
        deal = _waiting()

        with pytest.raises(DraftPending) as raised:
            run_send_pass(agent_draft=AgentDraft(active=True))

        payload = raised.value.payload
        assert payload["profile_text"] == "cto at acme"
        assert (payload["step"], payload["subject"]) == ("E0-opener.prompt.j2", "")
        assert PendingDraft.objects.filter(deal=deal).count() == 1
        assert deal.state == sending(0)

    def test_re_running_with_no_answer_re_asks_about_the_same_deal(self, no_mail):
        deal = _waiting()
        PendingDraft.objects.create(deal=deal)

        with pytest.raises(DraftPending) as raised:
            run_send_pass(agent_draft=AgentDraft(active=True))

        assert raised.value.payload["lead_id"] == deal.lead.public_id
        assert PendingDraft.objects.count() == 1

    def test_one_draft_is_handed_back_at_a_time_and_it_is_the_longest_due(self, no_mail):
        newer = _waiting("newer@acme.com")
        older = _waiting()
        put_in_state(older, sending(0), at=timezone.now() - timedelta(days=3))

        for _ in range(2):
            with pytest.raises(DraftPending) as raised:
                run_send_pass(agent_draft=AgentDraft(active=True))
            assert raised.value.payload["lead_id"] == older.lead.public_id

        assert PendingDraft.objects.get().deal == older
        assert newer.state == sending(0)


class TestResumingWithAnAnswer:
    def test_the_answer_sends_once_a_mailbox_is_free(self, no_mail, no_model):
        UserFactory()
        deal = _waiting()
        PendingDraft.objects.create(deal=deal)
        box = maillog.mailbox()

        with patch("openoutsend.planner.free_for_first_email", return_value=box), \
                patch("openoutsend.emails.sender._deliver"):
            result = run_send_pass(agent_draft=AgentDraft(
                active=True, subject="Quick one", body="Saw you're hiring."))

        assert result.opened == 1
        deal.refresh_from_db()
        assert deal.state == CONVERSING
        assert deal.email_subject == "Quick one"
        sent = Message.objects.get(direction=Direction.OUTBOUND)
        assert (sent.step, sent.subject, sent.body_text) == (0, "Quick one", "Saw you're hiring.")
        assert PendingDraft.objects.count() == 0

    def test_the_answer_is_kept_when_no_mailbox_is_free_yet(self, no_mail):
        deal = _waiting()
        PendingDraft.objects.create(deal=deal)

        with patch("openoutsend.planner.free_for_first_email", return_value=None):
            result = run_send_pass(agent_draft=AgentDraft(
                active=True, subject="Quick one", body="Saw you're hiring."))

        assert result.opened == 0
        pending = PendingDraft.objects.get(deal=deal)
        assert (pending.subject, pending.body) == ("Quick one", "Saw you're hiring.")
        assert deal.state == sending(0)

    def test_a_kept_answer_sends_on_a_later_bare_pass(self, no_mail):
        """The operator (or agent) need not repeat --subject/--body once given."""
        UserFactory()
        deal = _waiting()
        PendingDraft.objects.create(deal=deal, subject="Quick one",
                                    body="Saw you're hiring.")
        box = maillog.mailbox()

        with patch("openoutsend.planner.free_for_first_email", return_value=box), \
                patch("openoutsend.emails.sender._deliver"):
            result = run_send_pass(agent_draft=AgentDraft(active=True))

        assert result.opened == 1
        assert PendingDraft.objects.count() == 0

    def test_a_kept_answer_is_not_replaced_by_a_second_one(self, no_mail):
        deal = _waiting()
        PendingDraft.objects.create(deal=deal, subject="Quick one", body="Saw you're hiring.")

        with patch("openoutsend.planner.free_for_first_email", return_value=None):
            run_send_pass(agent_draft=AgentDraft(active=True, subject="Another", body="Hello."))

        pending = PendingDraft.objects.get(deal=deal)
        assert (pending.subject, pending.body) == ("Quick one", "Saw you're hiring.")

    def test_an_opener_answered_without_a_subject_is_refused(self, no_mail):
        deal = _waiting()
        PendingDraft.objects.create(deal=deal)

        with pytest.raises(OutsendError) as raised:
            run_send_pass(agent_draft=AgentDraft(active=True, body="Saw you're hiring."))

        assert not isinstance(raised.value, DraftPending)
        assert PendingDraft.objects.get(deal=deal).body == ""

    def test_a_send_that_fails_keeps_the_answer_for_the_next_pass(self, no_mail):
        UserFactory()
        deal = _waiting()
        PendingDraft.objects.create(deal=deal)
        box = maillog.mailbox()

        with patch("openoutsend.planner.free_for_first_email", return_value=box), \
                patch("openoutsend.emails.sender._deliver", side_effect=OSError("down")):
            result = run_send_pass(agent_draft=AgentDraft(
                active=True, subject="Quick one", body="Saw you're hiring."))

        assert (result.opened, result.failed) == (0, 1)
        assert PendingDraft.objects.get(deal=deal).body == "Saw you're hiring."
        assert deal.state == sending(0)

    def test_agent_draft_off_uses_the_llm_as_before(self, no_mail):
        UserFactory()
        deal = _waiting()
        box = maillog.mailbox()

        with patch("openoutsend.planner.free_for_first_email", side_effect=[box, None]), \
                patch("openoutsend.leads.summaries.materialize_profile_summary_if_missing"), \
                patch("openoutsend.core.agents.outreach.write_email",
                      return_value=Email(subject="Quick one", message="Hello.")) as write, \
                patch("openoutsend.emails.sender._deliver"):
            result = run_send_pass()

        write.assert_called_once()
        assert result.opened == 1
        assert deal.state == CONVERSING
        assert PendingDraft.objects.count() == 0


class TestALaterStep:
    def test_a_due_step_is_handed_back_with_its_rendered_prompt(self, no_mail, no_model,
                                                                tmp_path):
        deal = _past_its_opener(tmp_path, maillog.mailbox())

        with pytest.raises(DraftPending) as raised:
            run_send_pass(agent_draft=AgentDraft(active=True))

        payload = raised.value.payload
        assert payload["step"] == "E1-2d-steer.prompt.j2"
        assert payload["prompt"] == "Steer the list for portraits."
        assert payload["subject"] == "Which of the ten was wrong?"
        assert PendingDraft.objects.filter(deal=deal).count() == 1

    def test_a_body_alone_sends_it_in_the_thread_under_its_subject(self, no_mail, no_model,
                                                                   tmp_path):
        UserFactory()
        box = maillog.mailbox()
        deal = _past_its_opener(tmp_path, box)
        PendingDraft.objects.create(deal=deal)

        with patch("openoutsend.planner.free_thread_box", return_value=box), \
                patch("openoutsend.emails.sender._deliver"):
            result = run_send_pass(agent_draft=AgentDraft(active=True, body="Tell me who pays."))

        assert result.stepped == 1
        deal.refresh_from_db()
        assert deal.state == CONVERSING
        sent = Message.objects.get(thread=deal.thread, direction=Direction.OUTBOUND, step=1)
        assert sent.subject == "Which of the ten was wrong?"
        assert sent.body_text == "Tell me who pays."
        assert sent.in_reply_to == "opener@openoutreach.app"
        assert sent.prompt == "Steer the list for portraits."
        assert PendingDraft.objects.count() == 0

    def test_the_answer_is_kept_while_the_thread_box_is_busy(self, no_mail, tmp_path):
        deal = _past_its_opener(tmp_path, maillog.mailbox())
        PendingDraft.objects.create(deal=deal)

        with patch("openoutsend.planner.free_thread_box", return_value=None):
            result = run_send_pass(agent_draft=AgentDraft(active=True, body="Tell me who pays."))

        assert result.stepped == 0
        assert PendingDraft.objects.get(deal=deal).body == "Tell me who pays."
        assert deal.state == sending(1)

    def test_a_deal_stopped_while_its_draft_waited_is_not_written_to(self, no_mail, tmp_path):
        box = maillog.mailbox()
        deal = _past_its_opener(tmp_path, box, stop=True)
        PendingDraft.objects.create(deal=deal, body="Tell me who pays.")

        with patch("openoutsend.planner.free_thread_box", return_value=box), \
                patch("openoutsend.emails.sender._deliver") as deliver:
            result = run_send_pass(agent_draft=AgentDraft(active=True))

        deliver.assert_not_called()
        assert result.stepped == 0
        assert PendingDraft.objects.count() == 0
        deal.refresh_from_db()
        assert (deal.state, deal.outcome) == (COMPLETED, Outcome.STOPPED)


def _wrote_back(box, address="lead@acme.com"):
    """A deal in its conversation whose lead answered the opener — owed a reply."""
    deal = DealFactory(lead=LeadFactory(email=address), state=CONVERSING, mailbox=box,
                       email_subject="Ten leads", fields={"price": "$20"})
    thread = _in_thread(deal, box)
    maillog.outbound(box, thread=thread, to=address, step=0,
                     message_id=f"opener-{address}@openoutreach.app", subject="Ten leads",
                     sent_at=timezone.now() - timedelta(days=2))
    maillog.inbound(box, thread=thread, sender=address, body="Most are our clients already.",
                    sent_at=timezone.now() - timedelta(hours=1))
    return deal


class TestAReply:
    def test_is_handed_back_with_the_conversation(self, no_mail, no_model, site_config):
        deal = _wrote_back(maillog.mailbox())

        with pytest.raises(DraftPending) as raised:
            run_send_pass(agent_draft=AgentDraft(active=True))

        payload = raised.value.payload
        assert payload["step"] == "reply"
        assert payload["fields"] == {"price": "$20"}
        assert payload["subject"] == "Ten leads"
        assert "Most are our clients already." in payload["prompt"]
        assert PendingDraft.objects.filter(deal=deal).count() == 1

    def test_comes_before_a_due_step(self, no_mail, site_config, tmp_path):
        box = maillog.mailbox()
        _past_its_opener(tmp_path, box)
        replied = _wrote_back(box, "other@acme.com")

        with pytest.raises(DraftPending) as raised:
            run_send_pass(agent_draft=AgentDraft(active=True))

        assert raised.value.payload["lead_id"] == replied.lead.public_id
        assert PendingDraft.objects.get().deal == replied

    def test_re_running_with_no_answer_re_asks_about_the_same_reply(self, no_mail, site_config):
        box = maillog.mailbox()
        deal = _wrote_back(box)
        PendingDraft.objects.create(deal=deal)
        _wrote_back(box, "other@acme.com")

        with pytest.raises(DraftPending) as raised:
            run_send_pass(agent_draft=AgentDraft(active=True))

        assert raised.value.payload["lead_id"] == deal.lead.public_id
        assert PendingDraft.objects.count() == 1

    def test_a_body_sends_it_in_the_thread_whatever_the_guards_say(self, no_mail, no_model,
                                                                   site_config):
        box = maillog.mailbox(daily_limit=0, next_send_at=timezone.now() + timedelta(hours=1))
        deal = _wrote_back(box)
        PendingDraft.objects.create(deal=deal)

        with patch("openoutsend.core.sending_window.within_sending_window", return_value=False), \
                patch("openoutsend.emails.sender._deliver"):
            result = run_send_pass(agent_draft=AgentDraft(active=True, body="Which ones?"))

        assert result.answered == 1
        sent = Message.objects.filter(thread=deal.thread, direction=Direction.OUTBOUND) \
            .latest("pk")
        assert (sent.subject, sent.body_text) == ("Re: Ten leads", "Which ones?")
        assert sent.in_reply_to.startswith("in-")
        assert PendingDraft.objects.count() == 0

    def test_the_answer_is_the_turn_logged_as_decided_by_the_agent(self, no_mail, site_config):
        deal = _wrote_back(maillog.mailbox())
        PendingDraft.objects.create(deal=deal)

        with patch("openoutsend.emails.sender._deliver"):
            run_send_pass(agent_draft=AgentDraft(active=True, body="Which ones?"))

        move = machine_of(deal).history(deal.pk)[-1]
        assert (move.from_state, move.to_state) == (CONVERSING, CONVERSING)
        assert move.decided_by == "agent"
        assert deal.state == CONVERSING

    def test_a_refused_send_is_counted_and_does_not_raise(self, no_mail, site_config):
        deal = _wrote_back(maillog.mailbox())
        PendingDraft.objects.create(deal=deal)

        with patch("openoutsend.emails.sender._deliver", side_effect=OSError("down")):
            result = run_send_pass(agent_draft=AgentDraft(active=True, body="Which ones?"))

        assert (result.answered, result.failed) == (0, 1)
        assert PendingDraft.objects.get(deal=deal).body == "Which ones?"

    def test_waits_behind_a_draft_already_held_for_a_step(self, no_mail, site_config, tmp_path):
        """One draft at a time: the step handed over first is asked for again, and the
        reply is handed over once it has gone."""
        box = maillog.mailbox()
        stepping = _past_its_opener(tmp_path, box)
        _wrote_back(box, "other@acme.com")
        PendingDraft.objects.create(deal=stepping)

        with pytest.raises(DraftPending) as raised:
            run_send_pass(agent_draft=AgentDraft(active=True))

        assert raised.value.payload["lead_id"] == stepping.lead.public_id
        assert raised.value.payload["step"] == "E1-2d-steer.prompt.j2"
        assert PendingDraft.objects.get().deal == stepping

    def test_a_draft_owed_nothing_any_more_is_dropped(self, no_mail, site_config):
        """A reply answered meanwhile, outside the pass, is owed nothing: its draft is
        dropped and the next reply handed over."""
        box = maillog.mailbox()
        stale = _wrote_back(box)
        maillog.outbound(box, thread=stale.thread, message_id="answer@openoutreach.app",
                         subject="Re: Ten leads")
        PendingDraft.objects.create(deal=stale)
        replied = _wrote_back(box, "other@acme.com")

        with pytest.raises(DraftPending) as raised:
            run_send_pass(agent_draft=AgentDraft(active=True))

        assert raised.value.payload["lead_id"] == replied.lead.public_id
        assert PendingDraft.objects.get().deal == replied

    def test_a_draft_for_a_conversation_that_closed_is_dropped(self, no_mail, site_config):
        box = maillog.mailbox()
        closed = _wrote_back(box)
        PendingDraft.objects.create(deal=closed, body="Which ones?")
        put_in_state(closed, COMPLETED)

        with patch("openoutsend.emails.sender._deliver") as deliver:
            result = run_send_pass(agent_draft=AgentDraft(active=True))

        deliver.assert_not_called()
        assert result.answered == 0
        assert PendingDraft.objects.count() == 0
