"""Tests for the outreach agent's context builder + unified Jinja template."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from cold_outreach.emails.models import Thread
from cold_outreach.tests.emails import maillog
from cold_outreach.tests.factories import DealFactory, LeadFactory


@pytest.fixture
def deal_with_summaries(db, campaign):
    """A deal whose lead has facts and whose thread has learned some.

    The profile facts sit on the **lead** — they describe the person, not the campaign
    they are being written to about — while the chat facts sit on the deal, because a
    conversation is per campaign.
    """
    lead = LeadFactory(profile_summary={"facts": [
        "Senior engineer at Acme Corp.",
        "Based in Berlin, Germany.",
        "Speaks English and German.",
    ]})
    return DealFactory(
        lead=lead,
        campaign=campaign,
        thread=Thread.objects.create(mailbox=maillog.mailbox()),
        chat_summary={"facts": [
            "Lead is curious about pricing.",
            "Lead has a small team budget.",
        ]},
    )


def _msg(content, is_outgoing):
    m = MagicMock()
    m.body_text = content
    m.is_outbound = is_outgoing
    m.sent_at = None
    return m


class TestRenderSystemPrompt:
    def test_in_thread_includes_three_summary_blocks(self, db, campaign, deal_with_summaries):
        from cold_outreach.core.agents.outreach import _render_system_prompt

        recent = [_msg("Hi, what do you do?", is_outgoing=True), _msg("Sales tooling.", is_outgoing=False)]
        prompt = _render_system_prompt(deal_with_summaries, recent, is_first_touch=False)

        # Profile facts appear under the lead-knowledge block.
        assert "Senior engineer at Acme Corp." in prompt
        assert "Based in Berlin, Germany." in prompt
        # Chat facts appear under the conversation-knowledge block.
        assert "Lead is curious about pricing." in prompt
        # Verbatim recent messages appear in Me:/Lead: format.
        assert "Me: Hi, what do you do?" in prompt
        assert "Lead: Sales tooling." in prompt
        # The legacy flat fields are gone.
        assert "Headline:" not in prompt
        assert "Company:" not in prompt

    def test_in_thread_offers_the_three_reply_actions(self, db, campaign, deal_with_summaries):
        """In-thread choices are reply / suppress / complete. There is no `wait`:
        the agent only ever runs on a thread that has an unanswered reply, and
        silence is the absence of work rather than a decision."""
        from cold_outreach.core.agents.outreach import _render_system_prompt

        prompt = _render_system_prompt(deal_with_summaries, [], is_first_touch=False)

        assert "**send_message**" in prompt
        assert "**suppress**" in prompt
        assert "**mark_completed**" in prompt
        assert "**wait**" not in prompt
        assert "having an email conversation" in prompt

    def test_first_touch_drops_the_conversation_blocks(self, db, campaign, deal_with_summaries):
        """No thread yet — no chat summary, no transcript, and no complete/suppress choice."""
        from cold_outreach.core.agents.outreach import _render_system_prompt

        prompt = _render_system_prompt(deal_with_summaries, [], is_first_touch=True)

        # Lead facts still there; conversation facts are not.
        assert "Senior engineer at Acme Corp." in prompt
        assert "Lead is curious about pricing." not in prompt
        assert "Conversation So Far" not in prompt
        # The opener is forced to send and to carry a subject.
        assert "you always send" in prompt
        assert "`subject`" in prompt
        assert "**mark_completed**" not in prompt

    def test_both_ends_carry_the_research_framing(self, db, campaign, deal_with_summaries):
        from cold_outreach.core.agents.outreach import _render_system_prompt

        for first_touch in (True, False):
            prompt = _render_system_prompt(
                deal_with_summaries, [], is_first_touch=first_touch,
            )
            assert "You follow the Mom Test method." in prompt
            # Discovery is the default mode; pitching only on an explicit pull.
            assert "### Discovery (default, and where you stay)" in prompt
            assert "### Pitching (only when the lead pulls you there)" in prompt
            assert "Never volunteer the product" in prompt

    def test_an_unextracted_lead_falls_back_to_the_raw_profile_text(self, db, campaign):
        """The facts are a cache over ``profile_text``; until it is built, the text itself goes in.

        A lead is never described to the agent as *(none yet)* while the sentences the
        finder qualified them on are sitting in the row.
        """
        from cold_outreach.core.agents.outreach import _render_system_prompt

        lead = LeadFactory(profile_text="cto at acme, milan, 50 employees")
        deal = DealFactory(lead=lead, campaign=campaign,
                           thread=Thread.objects.create(mailbox=maillog.mailbox()))

        prompt = _render_system_prompt(deal, [], is_first_touch=False)

        assert "cto at acme, milan, 50 employees" in prompt
        # The conversation has taught us nothing yet, and says so.
        assert "(none yet)" in prompt
        assert "No recent messages." in prompt

    def test_a_lead_with_nothing_on_file_still_renders(self, db, campaign):
        from cold_outreach.core.agents.outreach import _render_system_prompt

        deal = DealFactory(lead=LeadFactory(profile_text=""), campaign=campaign)

        prompt = _render_system_prompt(deal, [], is_first_touch=True)

        assert "(nothing on file)" in prompt


class TestValidateOpener:
    def test_rejects_an_opener_without_a_subject(self):
        from cold_outreach.core.agents.outreach import OutreachDecision, _validate_opener

        decision = OutreachDecision(action="send_message", message="Hi.")
        with pytest.raises(ValueError, match="no subject"):
            _validate_opener(decision, "lead@corp.com")

    def test_rejects_an_opener_that_does_not_send(self):
        from cold_outreach.core.agents.outreach import OutreachDecision, _validate_opener

        decision = OutreachDecision(action="mark_completed", outcome="not_interested")
        with pytest.raises(ValueError, match="must send_message"):
            _validate_opener(decision, "lead@corp.com")

    def test_accepts_a_well_formed_opener(self):
        from cold_outreach.core.agents.outreach import OutreachDecision, _validate_opener

        decision = OutreachDecision(
            action="send_message", subject="quick question", message="Hi.",
        )
        _validate_opener(decision, "lead@corp.com")


class TestLoadRecentMessages:
    def test_returns_last_n_in_chronological_order(self, db, campaign, operator):
        from django.utils import timezone
        from datetime import timedelta

        from cold_outreach.core.agents.outreach import _load_recent_messages, RECENT_MESSAGES_WINDOW

        box = maillog.mailbox()
        thread = Thread.objects.create(mailbox=box)
        deal = DealFactory(lead=LeadFactory(), campaign=campaign, thread=thread)

        base = timezone.now()
        for i in range(RECENT_MESSAGES_WINDOW + 3):
            when = base + timedelta(minutes=i)
            if i % 2 == 0:
                maillog.outbound(box, thread=thread, body=f"msg-{i}", sent_at=when)
            else:
                maillog.inbound(box, thread=thread, body=f"msg-{i}", sent_at=when)

        recent = _load_recent_messages(deal)

        # Window respected and chronological order preserved.
        assert len(recent) == RECENT_MESSAGES_WINDOW
        contents = [m.body_text for m in recent]
        assert contents == sorted(contents, key=lambda c: int(c.split("-")[1]))
        # Returned the *latest* messages.
        assert contents[-1] == f"msg-{RECENT_MESSAGES_WINDOW + 2}"
