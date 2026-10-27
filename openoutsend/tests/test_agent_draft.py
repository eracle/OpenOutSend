# openoutsend/tests/test_agent_draft.py
"""``send --agent-draft`` — the calling agent writes the replies, openers and later
steps instead of AI_MODEL, and ``OUTSEND_REPLIES=hold`` keeps an unattended pass off
the replies.

Covers the pipeline layer only (``_send_agent_draft``, ``_answer_drafted_reply``); the
CLI's own flag parsing is covered by ``tests/test_cli.py``.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from openoutsend.core.agent_draft import AgentDraft
from openoutsend.emails.models import Direction, Message, Thread
from openoutsend.errors import DraftPending, OutsendError
from openoutsend.leads.models import DealState, PendingDraft
from openoutsend.send_pass import run_send_pass
from openoutsend.tests.emails import maillog
from openoutsend.tests.factories import DealFactory, LeadFactory, UserFactory

pytestmark = pytest.mark.django_db


def _waiting(address="lead@acme.com"):
    return DealFactory(lead=LeadFactory(email=address, profile_text="cto at acme"))


def _past_its_opener(tmp_path, box, **kwargs):
    """An EMAILED deal in a two-step sequence whose opener went out long enough ago
    that ``E1`` is due."""
    (tmp_path / "E0-open.prompt.j2").write_text("Open to {{ first_name }}.")
    (tmp_path / "E1-2d-steer.prompt.j2").write_text("Steer the list for {{ product }}.")
    thread = Thread.objects.create(mailbox=box)
    deal = DealFactory(
        lead=LeadFactory(email="lead@acme.com", first_name="Ada"),
        state=DealState.EMAILED, mailbox=box, thread=thread, step=0,
        sequence=str(tmp_path), fields={"product": "portraits"},
        email_subject="Which of the ten was wrong?", **kwargs,
    )
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


class TestStoppingForAnOpener:
    def test_a_fresh_deal_is_handed_back_instead_of_asking_the_llm(self, no_mail):
        deal = _waiting()

        with patch("openoutsend.core.agents.outreach.run_outreach_agent") as mock_llm:
            with pytest.raises(DraftPending) as raised:
                run_send_pass(agent_draft=AgentDraft(active=True))

        mock_llm.assert_not_called()
        assert raised.value.payload["profile_text"] == "cto at acme"
        assert PendingDraft.objects.filter(deal=deal).count() == 1

    def test_re_running_with_no_answer_re_asks_about_the_same_deal(self, no_mail):
        deal = _waiting()
        PendingDraft.objects.create(deal=deal)

        with pytest.raises(DraftPending) as raised:
            run_send_pass(agent_draft=AgentDraft(active=True))

        assert raised.value.payload["lead_id"] == deal.lead.public_id
        assert PendingDraft.objects.count() == 1


class TestResumingWithAnAnswer:
    def test_the_answer_sends_once_a_mailbox_is_free(self, no_mail):
        UserFactory()
        deal = _waiting()
        PendingDraft.objects.create(deal=deal)
        box = maillog.mailbox()

        with patch("openoutsend.planner.free_for_first_email", return_value=box), \
                patch("openoutsend.core.agents.outreach.run_outreach_agent") as mock_llm, \
                patch("openoutsend.emails.sender._deliver"):
            result = run_send_pass(agent_draft=AgentDraft(
                active=True, subject="Quick one", body="Saw you're hiring."))

        mock_llm.assert_not_called()
        assert result.opened == 1
        deal.refresh_from_db()
        assert deal.state == DealState.EMAILED
        assert deal.email_subject == "Quick one"
        assert PendingDraft.objects.count() == 0

    def test_the_answer_is_kept_when_no_mailbox_is_free_yet(self, no_mail):
        deal = _waiting()
        PendingDraft.objects.create(deal=deal)

        with patch("openoutsend.planner.free_for_first_email", return_value=None):
            result = run_send_pass(agent_draft=AgentDraft(
                active=True, subject="Quick one", body="Saw you're hiring."))

        assert result.opened == 0
        pending = PendingDraft.objects.get(deal=deal)
        assert pending.body == "Saw you're hiring."

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

    def test_an_opener_answered_without_a_subject_is_refused(self, no_mail):
        deal = _waiting()
        PendingDraft.objects.create(deal=deal)

        with pytest.raises(OutsendError) as raised:
            run_send_pass(agent_draft=AgentDraft(active=True, body="Saw you're hiring."))

        assert not isinstance(raised.value, DraftPending)
        assert PendingDraft.objects.get(deal=deal).body == ""

    def test_agent_draft_off_uses_the_llm_as_before(self, no_mail):
        _waiting()
        box = maillog.mailbox()

        with patch("openoutsend.planner.free_for_first_email",
                          side_effect=[box, None]), \
                patch("openoutsend.emails.steps.send.send_first_email") as send:
            send.return_value = DealState.EMAILED
            run_send_pass()

        send.assert_called_once()


class TestALaterStep:
    def test_a_due_step_is_handed_back_with_its_rendered_prompt(self, no_mail, tmp_path):
        deal = _past_its_opener(tmp_path, maillog.mailbox())

        with patch("openoutsend.core.agents.outreach.run_outreach_agent") as mock_llm:
            with pytest.raises(DraftPending) as raised:
                run_send_pass(agent_draft=AgentDraft(active=True))

        mock_llm.assert_not_called()
        payload = raised.value.payload
        assert payload["step"] == "E1-2d-steer.prompt.j2"
        assert payload["prompt"] == "Steer the list for portraits."
        assert payload["subject"] == "Which of the ten was wrong?"
        assert PendingDraft.objects.filter(deal=deal).count() == 1

    def test_a_body_alone_sends_it_in_the_thread_under_its_subject(self, no_mail, tmp_path):
        UserFactory()
        box = maillog.mailbox()
        deal = _past_its_opener(tmp_path, box)
        PendingDraft.objects.create(deal=deal)

        with patch("openoutsend.planner.free_thread_box", return_value=box), \
                patch("openoutsend.core.agents.outreach.run_outreach_agent") as mock_llm, \
                patch("openoutsend.emails.sender._deliver"):
            result = run_send_pass(agent_draft=AgentDraft(active=True, body="Tell me who pays."))

        mock_llm.assert_not_called()
        assert result.stepped == 1
        deal.refresh_from_db()
        assert deal.step == 1
        sent = Message.objects.get(thread=deal.thread, direction=Direction.OUTBOUND, step=1)
        assert sent.subject == "Which of the ten was wrong?"
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


def _wrote_back(box, address="lead@acme.com"):
    """An EMAILED deal whose lead answered the opener — owed a reply."""
    thread = Thread.objects.create(mailbox=box)
    deal = DealFactory(lead=LeadFactory(email=address), state=DealState.EMAILED,
                       mailbox=box, thread=thread, step=0, email_subject="Ten leads",
                       fields={"price": "$20"})
    maillog.outbound(box, thread=thread, to=address, step=0,
                     message_id=f"opener-{address}@openoutreach.app", subject="Ten leads",
                     sent_at=timezone.now() - timedelta(days=2))
    maillog.inbound(box, thread=thread, sender=address, body="Most are our clients already.",
                    sent_at=timezone.now() - timedelta(hours=1))
    return deal


class TestAReply:
    def test_is_handed_back_with_the_conversation(self, no_mail):
        deal = _wrote_back(maillog.mailbox())

        with patch("openoutsend.core.agents.outreach.run_outreach_agent") as mock_llm:
            with pytest.raises(DraftPending) as raised:
                run_send_pass(agent_draft=AgentDraft(active=True))

        mock_llm.assert_not_called()
        payload = raised.value.payload
        assert payload["step"] == "reply"
        assert payload["fields"] == {"price": "$20"}
        assert "Most are our clients already." in payload["prompt"]
        assert PendingDraft.objects.filter(deal=deal).count() == 1

    def test_comes_before_a_due_step(self, no_mail, tmp_path):
        box = maillog.mailbox()
        _past_its_opener(tmp_path, box)
        replied = _wrote_back(box, "other@acme.com")

        with pytest.raises(DraftPending) as raised:
            run_send_pass(agent_draft=AgentDraft(active=True))

        assert raised.value.payload["lead_id"] == replied.lead.public_id

    def test_a_body_sends_it_in_the_thread_whatever_the_guards_say(self, no_mail):
        UserFactory()
        box = maillog.mailbox()
        deal = _wrote_back(box)
        PendingDraft.objects.create(deal=deal)

        with patch("openoutsend.core.agents.outreach.run_outreach_agent") as mock_llm, \
                patch("openoutsend.emails.sender._deliver"):
            result = run_send_pass(agent_draft=AgentDraft(active=True, body="Which ones?"))

        mock_llm.assert_not_called()
        assert result.answered == 1
        sent = Message.objects.filter(thread=deal.thread, direction=Direction.OUTBOUND) \
            .latest("pk")
        assert (sent.subject, sent.body_text) == ("Re: Ten leads", "Which ones?")
        assert sent.in_reply_to.startswith("in-")
        assert PendingDraft.objects.count() == 0

    def test_a_refused_send_is_counted_and_does_not_raise(self, no_mail):
        UserFactory()
        deal = _wrote_back(maillog.mailbox())
        PendingDraft.objects.create(deal=deal)

        with patch("openoutsend.emails.sender._deliver", side_effect=OSError("down")):
            result = run_send_pass(agent_draft=AgentDraft(active=True, body="Which ones?"))

        assert (result.answered, result.failed) == (0, 1)


class TestHeldReplies:
    def test_an_unattended_pass_leaves_the_reply_and_sends_the_steps(
            self, no_mail, tmp_path, monkeypatch):
        monkeypatch.setenv("OUTSEND_REPLIES", "hold")
        box = maillog.mailbox()
        stepping = _past_its_opener(tmp_path, box)
        replied = _wrote_back(box, "other@acme.com")

        with patch("openoutsend.core.agents.outreach.run_outreach_agent") as mock_llm, \
                patch("openoutsend.emails.steps.send.send_next_step",
                      return_value=DealState.EMAILED) as step, \
                patch("openoutsend.planner.free_thread_box", return_value=box):
            result = run_send_pass()

        mock_llm.assert_not_called()
        assert step.call_args.args[0] == stepping
        assert result.answered == 0
        assert "held for an agent" in result.holding
        assert not Message.objects.filter(thread=replied.thread, direction=Direction.OUTBOUND,
                                          sent_at__gt=timezone.now() - timedelta(hours=1))

    def test_an_unknown_setting_stops_the_pass(self, no_mail, monkeypatch):
        monkeypatch.setenv("OUTSEND_REPLIES", "later")
        _wrote_back(maillog.mailbox())

        with pytest.raises(OutsendError, match="OUTSEND_REPLIES"):
            run_send_pass()
