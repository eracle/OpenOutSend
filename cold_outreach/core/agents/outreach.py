# cold_outreach/core/agents/outreach.py
"""The outreach agent: one prompt, one decision type, every stage of the thread.

There is a single conversational agent. The cold open, a follow-up and every later
reply are the same voice doing the same job — Mom Test research, not selling — so all
three render from one template (``outreach_agent.j2``), which branches on the stage
the caller names:

- **open** (no thread): the agent must ``send_message`` and must supply a ``subject``.
  ``emails/steps/send.py`` sends it and records the thread root.
- **follow_up** (a thread nobody has answered): ``send_message`` in the same thread
  under the same subject, or ``mark_completed`` if the record says this person is
  plainly the wrong fit. ``emails/steps/follow_up.py`` executes it.
- **reply** (they wrote back): ``send_message`` / ``mark_completed`` / ``suppress``.
  ``emails/steps/reply.py`` executes the choice.

**The agent never decides *when*.** There is no ``wait`` action and no follow-up
interval to choose: the pools in ``leads/pools.py`` decide who is due from timestamps
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


# Number of trailing verbatim messages the agent sees alongside the rolling
# chat_summary. Older turns live in the summary fact list; the recency window
# preserves literal phrasing for the turns that matter most when composing
# the next reply.
RECENT_MESSAGES_WINDOW = 6


# ── The three stages of a conversation ────────────────────────────

# What the agent is being asked to write. `OPEN` and `FOLLOW_UP` are **cold** — nobody
# has answered, the same hard rules apply to both, and both are written from the same
# prompt line so a sequence keeps one voice. `REPLY` is not cold: somebody wrote to us,
# and the message answers what they said rather than making a move picked in advance.
OPEN = "open"
FOLLOW_UP = "follow_up"
REPLY = "reply"

COLD_STAGES = (OPEN, FOLLOW_UP)


# ── The hard rules for a cold message ─────────────────────────────

# Most of the LinkedIn-DM *feel* is length and ask-shape rather than depth of
# personalisation, and a cold email has one job small enough to do in a paragraph.
# A ceiling rather than a target: a prompt line that writes 30 words is not in breach.
# It binds a follow-up as hard as an opener — a chaser that runs longer than the
# message it is chasing is the exact thing nobody answers.
COLD_WORD_CEILING = 75


def run_outreach_agent(deal, prompt_line=None, stage=None) -> OutreachDecision:
    """Decide the next move for ``deal`` at ``stage``.

    The caller has already folded any new inbound messages into ``deal.chat_summary``
    (``emails/steps/reply.py``), so this reads the conversation store rather than the
    mailbox — no IMAP here.

    ``stage`` says which of the three things is being written, and the **caller** names
    it rather than this function deriving it: each step knows exactly what it is, and a
    derivation here would be a second copy of the pool predicates in `leads/pools.py`,
    free to drift from them. It defaults to the one case that is unambiguous from the
    row alone — no thread means nothing has been sent.

    ``prompt_line`` is the move the cold message makes (``core/prompt_lines.py``). A
    follow-up is given the **same line the opener used**, so a sequence reads as one
    person, and so a reply can be attributed to one line rather than to a mixture.
    Ignored on a reply.

    **The rules live in the prompt, not in a check after the fact.** ``outreach_agent.j2``
    states the word ceiling, the no-link/no-meeting/no-em-dash rules and the sourcing
    constraint directly (see its "Rules this email cannot break" section) — there is no
    generator-side validation of a cold message's content, and no retry. A draft is sent
    as written.
    """
    public_id = deal.lead.public_id
    stage = stage or (OPEN if not deal.thread_id else REPLY)

    recent = [] if stage == OPEN else _load_recent_messages(deal)
    system_prompt = _render_system_prompt(deal, recent, stage, prompt_line)

    agent = Agent(
        get_llm_model(),
        output_type=OutreachDecision,
        model_settings={"temperature": 0.7, "timeout": 60},
    )
    decision = _run_once(agent, system_prompt, public_id)
    if stage == OPEN:
        _validate_opener(decision, public_id)
    logger.info("outreach agent for %s: %s %s (prompt line: %s)",
                public_id, stage, decision.action,
                prompt_line.id if prompt_line else "none")
    return decision


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


# ── Prompt context ────────────────────────────────────────────────


def _render_system_prompt(deal, recent_messages: list, stage: str, prompt_line=None) -> str:
    """Render the outreach prompt for whichever stage of the conversation we're at."""
    now = timezone.now()
    thread_context = {} if stage == OPEN else {
        "chat_summary": _format_facts(deal.chat_summary),
        "recent_messages": _format_recent_messages(recent_messages, now),
        "today": now.strftime("%Y-%m-%d"),
        "business_days_since_last_outgoing": _business_days_since_last_outgoing(recent_messages, now),
    }
    return render(
        "outreach_agent.j2",
        **base_context(deal),
        stage=stage,
        is_first_touch=stage == OPEN,
        is_cold=stage in COLD_STAGES,
        is_follow_up=stage == FOLLOW_UP,
        prompt_line=prompt_line.prompt if (prompt_line and stage in COLD_STAGES) else "",
        word_ceiling=COLD_WORD_CEILING,
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
