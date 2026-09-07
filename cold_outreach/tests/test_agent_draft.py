# cold_outreach/tests/test_agent_draft.py
"""``send --agent-draft`` — the calling agent writes the opener instead of AI_MODEL.

Covers the pipeline layer only (``_open_with_agent_draft``); the CLI's own flag
parsing is covered by ``tests/test_cli.py``.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from cold_outreach.core.agent_draft import AgentDraft
from cold_outreach.emails.models import Mailbox
from cold_outreach.errors import DraftPending
from cold_outreach.leads.models import DealState, PendingDraft
from cold_outreach.send_pass import PassResult, run_send_pass
from cold_outreach.tests.emails import maillog
from cold_outreach.tests.factories import DealFactory, LeadFactory, UserFactory

pytestmark = pytest.mark.django_db


def _waiting(address="lead@acme.com"):
    return DealFactory(lead=LeadFactory(email=address, profile_text="cto at acme"))


@pytest.fixture
def no_mail():
    """Neither the read nor the warmth measurement matter to this step."""
    with patch("cold_outreach.emails.mail_pass.run_mail_pass", return_value=(0, 0, 0)), \
            patch("cold_outreach.emails.warmth.measure_pool"):
        yield


class TestStoppingForAnOpener:
    def test_a_fresh_deal_is_handed_back_instead_of_asking_the_llm(self, no_mail):
        deal = _waiting()

        with patch("cold_outreach.core.agents.outreach.run_outreach_agent") as mock_llm:
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

        with patch.object(Mailbox.objects, "free_for_first_email", return_value=box), \
                patch("cold_outreach.core.agents.outreach.run_outreach_agent") as mock_llm, \
                patch("cold_outreach.emails.sender._deliver"):
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

        with patch.object(Mailbox.objects, "free_for_first_email", return_value=None):
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

        with patch.object(Mailbox.objects, "free_for_first_email", return_value=box), \
                patch("cold_outreach.emails.sender._deliver"):
            result = run_send_pass(agent_draft=AgentDraft(active=True))

        assert result.opened == 1
        assert PendingDraft.objects.count() == 0

    def test_agent_draft_off_uses_the_llm_as_before(self, no_mail):
        _waiting()
        box = maillog.mailbox()

        with patch.object(Mailbox.objects, "free_for_first_email",
                          side_effect=[box, None]), \
                patch("cold_outreach.emails.steps.send.send_first_email") as send:
            send.return_value = DealState.EMAILED
            run_send_pass()

        send.assert_called_once()
