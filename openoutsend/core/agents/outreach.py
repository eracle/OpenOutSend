# openoutsend/core/agents/outreach.py
"""The outreach agent: one call, one decision type, every message there is to write.

Every message is the same call: the frame (``frame.j2``) as the system prompt, then the
whole history with this lead (``core/agents/history.py``), then the prompt for the
message being written. What changes between an opener, a follow-up and a reply is only
that last prompt, and each lives with the step that sends it:

- **a sequence step** — a step file (``core/sequences.py``); cold's opener is
  ``sequences/cold/E0-opener.md``. ``emails/steps/send.py`` sends it.
- **a follow-up** — ``emails/steps/follow_up.FOLLOW_UP_PROMPT``: ``send_message`` in the
  same thread, or ``mark_completed`` if the record says this person is plainly the
  wrong fit.
- **a reply** — ``emails/steps/reply.REPLY_PROMPT``: ``send_message`` / ``mark_completed``
  / ``suppress``.

**The rules live in the prompts, not in a check after the fact.** The frame carries
none, so a step that needs a link just says so, and cold's word ceiling and no-link
rule sit in cold's own prompts. There is no generator-side validation of content and
no retry; a draft is sent as written.

**The agent never decides *when*.** There is no ``wait`` action and no follow-up
interval to choose: the planner (``planner.py``) decides who is due from timestamps
in the mail log, and the agent is only ever asked what to write. An earlier version
let it re-arm its own countdown, and a ``wait`` verdict that failed to push the next
action out re-read one unchanged context five times inside a single window.

``suppress`` is the *worded* unsubscribe — "take me off your list", "stop
emailing me". It threads like any other reply, so the box-wide alias scan in
``emails/classify.py`` can never see it, and the agent reading every reply already
can. It is a stronger statement than ``not_interested``: it ends the thread and
suppresses the person account-wide, for good.

**It is one decision the model can word two ways**, so both are read as one. The
action is ``suppress`` and the outcome it records is ``unsubscribed``; a decision that
names that outcome while reaching for ``mark_completed`` is saying the same thing, and
``emails/steps/reply.py`` honours it identically. Believing only the action would let a
model that answered the question correctly still leave the address off the list.

Single LLM call with structured output — no tool-calling loop.
"""
from __future__ import annotations

import logging
from typing import Literal

from django.utils import timezone
from pydantic import BaseModel, Field, model_validator
from pydantic_ai import Agent

from openoutsend.core.agents.history import history
from openoutsend.core.agents.prompt import base_context, render
from openoutsend.core.llm import get_llm_model, run_agent_sync

logger = logging.getLogger(__name__)


class OutreachDecision(BaseModel):
    """Structured output from the outreach agent, at either end of the thread."""

    action: Literal["send_message", "mark_completed", "suppress"] = Field(
        description=(
            "What to do next for this lead. The first email in a thread is always send_message. "
            "Use suppress when the lead asked to stop being contacted."
        ),
    )
    subject: str | None = Field(
        default=None,
        description=(
            "Subject line. Required on the first email of a thread — short and specific, "
            "like a real person wrote it, not salesy. Omit when replying in an existing thread."
        ),
    )
    message: str | None = Field(
        default=None,
        description="The email to send. Required when action='send_message'. A few short sentences, no signature.",
    )
    outcome: Literal[
        "converted", "not_interested", "wrong_fit", "no_budget",
        "has_solution", "bad_timing", "unresponsive", "unsubscribed",
    ] | None = Field(
        default=None,
        description=(
            "Why the conversation ended. Required when action='mark_completed'. "
            "'unsubscribed' belongs to action='suppress', which records it for you — "
            "naming it here says the same thing and is honoured the same way."
        ),
    )
    @model_validator(mode="after")
    def _check_required_fields(self):
        if self.action == "send_message" and not self.message:
            raise ValueError("message is required when action='send_message'")
        if self.action == "mark_completed" and not self.outcome:
            raise ValueError("outcome is required when action='mark_completed'")
        return self


def run_outreach_agent(deal, prompt: str, *, opener: bool = False) -> OutreachDecision:
    """Write the next message to ``deal``'s lead from ``prompt``, after the whole history.

    ``opener`` says this message opens a thread, so it must be a ``send_message`` with
    a subject of its own; the caller knows, and a derivation here would be a second
    copy of the planner's pools, free to drift from them.
    """
    public_id = deal.lead.public_id
    agent = Agent(
        get_llm_model(),
        system_prompt=render_frame(deal),
        output_type=OutreachDecision,
        # No temperature: the current Claude models reject one that isn't their default.
        model_settings={"timeout": 60},
    )
    decision = _run_once(agent, conversation(deal, prompt), public_id)
    if opener:
        _validate_opener(decision, public_id)
    logger.info("outreach agent for %s: %s", public_id, decision.action)
    return decision


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


def _run_once(agent, prompt: str, public_id: str) -> OutreachDecision:
    """One agent call, with an unparseable answer raised rather than returned as None."""
    decision = run_agent_sync(agent.run(prompt)).output
    if decision is None:
        raise RuntimeError(f"LLM returned unparseable response for outreach to {public_id}")
    return decision


def _validate_opener(decision: OutreachDecision, public_id: str) -> None:
    """A first touch must be a sendable email with its own subject."""
    if decision.action != "send_message":
        raise ValueError(f"opener for {public_id} must send_message, got {decision.action}")
    if not decision.subject:
        raise ValueError(f"opener for {public_id} has no subject")
