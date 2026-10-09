"""The outreach agent: the frame, the conversation it is handed, and the opener check.

Every message is the same call — the frame as the system prompt, then the history with
the lead, then the prompt for this email — so these tests check each part once rather
than one template per stage.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from openoutsend.emails.models import Thread
from openoutsend.tests.emails import maillog
from openoutsend.tests.factories import DealFactory, LeadFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def deal(site_config):
    """A deal whose lead has facts on file."""
    lead = LeadFactory(email="lead@acme.com", first_name="Anna", profile_summary={"facts": [
        "Senior engineer at Acme Corp.",
        "Based in Berlin, Germany.",
    ]})
    return DealFactory(lead=lead)


# ── The frame ─────────────────────────────────────────────────────


class TestTheFrame:
    def test_it_carries_the_install_and_the_lead(self, deal):
        from openoutsend.core.agents.outreach import render_frame
        from openoutsend.tests.conftest import CAMPAIGN_TARGET, PRODUCT_DOCS

        frame = render_frame(deal)

        assert PRODUCT_DOCS in frame
        assert CAMPAIGN_TARGET in frame
        assert "Senior engineer at Acme Corp." in frame
        assert f"Today is {timezone.now():%Y-%m-%d}" in frame

    def test_it_carries_no_rules_of_its_own(self, deal):
        """The rules come with each prompt, so a step that needs a link can just say so."""
        from openoutsend.core.agents.outreach import render_frame

        frame = render_frame(deal)

        assert "No link of any kind" not in frame
        assert "Mom Test" not in frame

    def test_an_unextracted_lead_falls_back_to_the_raw_profile_text(self, site_config):
        """The facts are a cache over ``profile_text``; until it is built, the text itself goes in."""
        from openoutsend.core.agents.outreach import render_frame

        deal = DealFactory(lead=LeadFactory(profile_text="cto at acme, milan, 50 employees"))

        assert "cto at acme, milan, 50 employees" in render_frame(deal)

    def test_a_lead_with_nothing_on_file_still_renders(self, site_config):
        from openoutsend.core.agents.outreach import render_frame

        deal = DealFactory(lead=LeadFactory(profile_text=""))

        assert "(nothing on file)" in render_frame(deal)


# ── The conversation ──────────────────────────────────────────────


class TestTheConversation:
    def test_a_first_email_is_its_prompt_alone(self, deal):
        from openoutsend.core.agents.outreach import conversation

        message = conversation(deal, "Say hello.")

        assert message == "## Write this email now\n\nSay hello."

    def test_the_prompt_comes_after_everything_so_far(self, deal):
        from openoutsend.core.agents.outreach import conversation

        box = maillog.mailbox()
        deal.thread = Thread.objects.create(mailbox=box)
        deal.save()
        maillog.outbound(box, thread=deal.thread, body="Hello Anna.", step=0,
                         prompt="Say hello.", sent_at=timezone.now() - timedelta(days=1))
        maillog.inbound(box, thread=deal.thread, body="Hi, who is this?")

        message = conversation(deal, "Answer them.")

        assert message.index("Hello Anna.") < message.index("Hi, who is this?") \
            < message.index("Answer them.")
        assert message.endswith("Answer them.")


# ── The opener check ──────────────────────────────────────────────


class TestValidateOpener:
    def test_rejects_an_opener_without_a_subject(self):
        from openoutsend.core.agents.outreach import OutreachDecision, _validate_opener

        decision = OutreachDecision(action="send_message", message="Hi.")
        with pytest.raises(ValueError, match="no subject"):
            _validate_opener(decision, "lead@corp.com")

    def test_rejects_an_opener_that_does_not_send(self):
        from openoutsend.core.agents.outreach import OutreachDecision, _validate_opener

        decision = OutreachDecision(action="mark_completed", outcome="not_interested")
        with pytest.raises(ValueError, match="must send_message"):
            _validate_opener(decision, "lead@corp.com")

    def test_accepts_a_well_formed_opener(self):
        from openoutsend.core.agents.outreach import OutreachDecision, _validate_opener

        decision = OutreachDecision(
            action="send_message", subject="quick question", message="Hi.",
        )
        _validate_opener(decision, "lead@corp.com")


# ── Cold's opener, rendered ───────────────────────────────────────


class TestTheColdOpener:
    """``sequences/cold/E0-opener.md`` is today's first-touch prompt, now a step file."""

    def _render(self, deal, prompt_line):
        from openoutsend.core.sequences import cold
        from openoutsend.emails.steps.send import step_fields

        return cold().first.render(**step_fields(deal, prompt_line))

    def test_it_draws_a_prompt_line(self):
        from openoutsend.core.sequences import cold

        assert cold().first.uses_prompt_line

    def test_the_line_reaches_the_prompt(self, deal):
        from openoutsend.core.prompt_lines import choose

        line = choose("plain-ask")

        assert line.prompt in self._render(deal, line)

    def test_the_hard_rules_are_in_it_whatever_the_line_says(self, deal):
        """They live in the step file, so no prompt-line file has to remember them."""
        from openoutsend.core.prompt_lines import choose

        prompt = self._render(deal, choose("plain-ask"))

        assert "No link of any kind" in prompt
        assert "No meeting request" in prompt
        assert "At most 75 words" in prompt
        assert "You follow the Mom Test method." in prompt

    def test_an_install_with_no_lines_still_renders_an_opener(self, deal):
        assert "one genuine discovery question" in self._render(deal, None)
