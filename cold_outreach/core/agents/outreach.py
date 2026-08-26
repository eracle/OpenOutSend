# cold_outreach/core/agents/outreach.py
"""The outreach agent: one prompt, one decision type, both ends of the thread.

There is a single conversational agent. The cold open and every later reply are
the same voice doing the same job — Mom Test research, not selling — so they
render from one template (``outreach_agent.j2``) which branches on the only thing
that actually differs: whether a thread exists yet.

- **first touch** (no thread): the agent must ``send_message`` and must supply a
  ``subject``. ``emails/steps/send.py`` sends it and records the thread root.
- **in thread**: the agent only ever runs on a thread the lead has *replied* to —
  the mail pass has already written the reply — so it picks ``send_message`` /
  ``mark_completed`` / ``suppress``. ``emails/steps/reply.py`` executes the choice.

There is no ``wait`` and no follow-up interval, because nobody is chased: silence
is not a decision the agent gets to make, it is simply the absence of work.

``suppress`` is the *worded* unsubscribe — "take me off your list", "stop
emailing me". It threads like any other reply, so the box-wide alias scan in
``emails/classify.py`` can never see it, and the agent reading every reply already
can. It is a stronger statement than ``not_interested``: it ends the thread and
suppresses the person account-wide, across every campaign.

Single LLM call with structured output — no tool-calling loop.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Literal

from django.utils import timezone
from pydantic import BaseModel, Field, model_validator
from pydantic_ai import Agent

from cold_outreach.core.agents.prompt import _format_facts, base_context, render
from cold_outreach.core.business_time import business_days_between
from cold_outreach.core.llm import get_llm_model, run_agent_sync

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
        "has_solution", "bad_timing", "unresponsive",
    ] | None = Field(
        default=None,
        description="Why the conversation ended. Required when action='mark_completed'.",
    )
    @model_validator(mode="after")
    def _check_required_fields(self):
        if self.action == "send_message" and not self.message:
            raise ValueError("message is required when action='send_message'")
        if self.action == "mark_completed" and not self.outcome:
            raise ValueError("outcome is required when action='mark_completed'")
        return self


# Number of trailing verbatim messages the agent sees alongside the rolling
# chat_summary. Older turns live in the summary fact list; the recency window
# preserves literal phrasing for the turns that matter most when composing
# the next reply.
RECENT_MESSAGES_WINDOW = 6


def run_outreach_agent(deal) -> OutreachDecision:
    """Decide the next move for ``deal`` — the cold open or the answer to a reply.

    The caller has already folded any new inbound messages into ``deal.chat_summary``
    (``emails/steps/reply.py``), so this reads the conversation store rather than the
    mailbox — no IMAP here. On a first touch there is nothing to read, and the
    decision is validated to be a sendable opener with a subject.
    """
    public_id = deal.lead.profile_url
    is_first_touch = not deal.thread_id

    if not is_first_touch:
        _log_chat_facts(public_id, deal)

    recent = [] if is_first_touch else _load_recent_messages(deal)
    system_prompt = _render_system_prompt(deal, recent, is_first_touch)

    agent = Agent(
        get_llm_model(),
        output_type=OutreachDecision,
        model_settings={"temperature": 0.7, "timeout": 60},
    )
    decision = run_agent_sync(agent.run(system_prompt)).output
    if decision is None:
        raise RuntimeError(f"LLM returned unparseable response for outreach to {public_id}")
    if is_first_touch:
        _validate_opener(decision, public_id)

    logger.info("outreach agent for %s: %s", public_id, decision.action)
    return decision


def _validate_opener(decision: OutreachDecision, public_id: str) -> None:
    """A first touch must be a sendable email with its own subject."""
    if decision.action != "send_message":
        raise ValueError(f"opener for {public_id} must send_message, got {decision.action}")
    if not decision.subject:
        raise ValueError(f"opener for {public_id} has no subject")


# ── Prompt context ────────────────────────────────────────────────


def _render_system_prompt(deal, recent_messages: list, is_first_touch: bool) -> str:
    """Render the outreach prompt for whichever end of the thread we're at."""
    now = timezone.now()
    thread_context = {} if is_first_touch else {
        "chat_summary": _format_facts(deal.chat_summary),
        "recent_messages": _format_recent_messages(recent_messages, now),
        "today": now.strftime("%Y-%m-%d"),
        "business_days_since_last_outgoing": _business_days_since_last_outgoing(recent_messages, now),
    }
    return render(
        "outreach_agent.j2",
        **base_context(deal),
        is_first_touch=is_first_touch,
        **thread_context,
    )


def _load_recent_messages(deal, limit: int = RECENT_MESSAGES_WINDOW) -> list:
    """The thread's last `limit` **turns**, in chronological order.

    The recency window of verbatim turns the agent sees alongside the rolling
    ``chat_summary`` — the opener plus any replies read from the mailbox. Turns
    only, so a non-delivery report cannot reach the agent's reasoning and be
    answered as though a person had written it.
    """
    if not deal.thread_id:
        return []
    return list(reversed(list(deal.thread.turns().order_by("-sent_at", "-pk")[:limit])))


def _format_recent_messages(messages: list, now: datetime) -> str:
    """Render the last few turns as a timestamped transcript."""
    if not messages:
        return "No recent messages."
    lines = []
    for m in messages:
        content = (m.body_text or "").strip()
        if not content:
            continue
        speaker = "Me" if m.is_outbound else "Lead"
        prefix = f"{speaker} ({_humanize_age(m.sent_at, now)})" if m.sent_at else speaker
        lines.append(f"{prefix}: {content}")
    return "\n".join(lines) or "No recent messages."


def _humanize_age(when: datetime, now: datetime) -> str:
    """Render `when` as a coarse age relative to `now` (e.g. ``3d ago``)."""
    delta = now - when
    if delta < timedelta(hours=1):
        return f"{max(int(delta.total_seconds() // 60), 1)}m ago"
    if delta < timedelta(days=1):
        return f"{int(delta.total_seconds() // 3600)}h ago"
    return f"{delta.days}d ago"


def _business_days_since_last_outgoing(messages: list, now: datetime) -> int | None:
    """Whole working days since the most recent outgoing message, or None if there are none.

    Weekends don't count — a Friday send read on Monday is one working day old,
    which is the gap the agent should pace against.
    """
    timestamps = [m.sent_at for m in messages if m.is_outbound and m.sent_at]
    if not timestamps:
        return None
    return business_days_between(max(timestamps), now)


def _log_chat_facts(public_id: str, deal) -> None:
    """Log the mem0 chat facts the agent is working with."""
    chat_facts = (deal.chat_summary or {}).get("facts", [])
    if not chat_facts:
        return
    lines = [f"chat facts for {public_id}:"]
    lines.extend(f"  • {f}" for f in chat_facts)
    logger.info("\n".join(lines))
