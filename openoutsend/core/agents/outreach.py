# openoutsend/core/agents/outreach.py
"""The outreach agent: one call that writes one email of a sequence.

Every step written from a prompt is the same call: the frame (``frame.j2``) as the
system prompt, then the whole history with this lead (``core/agents/history.py``), then
the step's prompt as rendered (``core/sequences.py``; cold's opener is
``sequences/cold/E0-opener.prompt.j2``). ``emails/steps/send.py`` sends what comes back.

**It writes and decides nothing.** Which step a deal is owed is its state and when it
is due is that state's wait (``leads/lifecycle.py``), so the model is only ever asked
for the words. Whether a conversation goes on or ends is asked once the steps have
gone, of the turn that answers a reply (``leads/conversing.py``).

**The rules live in the prompts, not in a check after the fact.** The frame carries
only the one every message shares (something genuine from the past), so a step that
needs a link just says so, and cold's word ceiling and no-link rule sit in cold's own
prompt. There is no generator-side validation of content and no retry; a draft is sent
as written.

Single LLM call with structured output — no tool-calling loop.
"""
from __future__ import annotations

import logging

from django.utils import timezone
from pydantic import BaseModel, Field
from pydantic_ai import Agent

from openoutsend.core.agents.history import history
from openoutsend.core.agents.prompt import base_context, render
from openoutsend.core.llm import get_llm_model, run_agent_sync

logger = logging.getLogger(__name__)


class Email(BaseModel):
    """One email of a sequence, as the model wrote it."""

    subject: str | None = Field(
        default=None,
        description=(
            "Subject line. Required on the first email of a thread — short and specific, "
            "like a real person wrote it, not salesy. Omit when writing in an existing thread."
        ),
    )
    message: str = Field(
        min_length=1,
        description="The email to send. A few short sentences, no signature.",
    )


def write_email(deal, prompt: str, *, opener: bool = False) -> Email:
    """Write the next email to ``deal``'s lead from ``prompt``, after the whole history.

    ``opener`` says this email opens a thread, so it must carry a subject of its own;
    the caller knows, because a deal with no thread has not been written to.
    """
    public_id = deal.lead.public_id
    agent = Agent(
        get_llm_model(),
        system_prompt=render_frame(deal),
        output_type=Email,
        # No temperature: the current Claude models reject one that isn't their default.
        model_settings={"timeout": 60},
    )
    email = run_agent_sync(agent.run(conversation(deal, prompt))).output
    if email is None:
        raise RuntimeError(f"LLM returned unparseable response for outreach to {public_id}")
    if opener and not email.subject:
        raise ValueError(f"opener for {public_id} has no subject")
    logger.info("outreach agent wrote to %s", public_id)
    return email


def render_frame(deal) -> str:
    """The system prompt every message to *deal*'s lead is written inside."""
    return render("frame.j2", **base_context(deal),
                  today=timezone.now().strftime("%Y-%m-%d"))


def conversation(deal, prompt: str) -> str:
    """The user message: the history with this lead, then the prompt for this email."""
    so_far = history(deal.lead, timezone.now())
    now = f"## Write this email now\n\n{prompt}"
    if not so_far:
        return now
    return f"## Everything so far\n\n{so_far}\n\n{now}"
