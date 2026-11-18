"""A deal's conversation: what the model answers from, and what a turn records.

Once every step of a sequence has gone, its deal rests in `conversing`
(`leads/lifecycle.py`), a conversation over its thread (`machines/`). The model reads
everything that passed and takes one turn: it answers and the deal stays, or it ends
the conversation and says why.

**What it answers from** is the frame every message to this lead is written inside
(who is writing, the product, what we know about them), then the sequence's own
conversation prompt (`core/sequences.py`), or the built-in one below when the folder
carries none.

**The built-in prompt is written for cold outreach**: learn about the lead, never
volunteer the product. A sequence written to people who know what they were sent
carries its own.

**A turn that ends says why** (`DealTurn.outcome`), and that is written on the deal
with the move. `unsubscribed` also puts the address on the suppression list for good,
which ends the lead's other open deals; nothing is sent to somebody who asked to stop.
"""
from __future__ import annotations

import logging
from typing import Literal

from pydantic import Field, model_validator

from openoutsend.leads.states import CONVERSING
from openoutsend.machines import Turn

logger = logging.getLogger(__name__)

BUILT_IN_PROMPT = """\
This lead has replied. You are here because their newest message has not been answered yet, so there is always something to respond to.

## Strategy

You follow the Mom Test method. Your conversations have two modes:

### Discovery (default, and where you stay)
Your goal is to understand the lead's world — their problems, workflows, tools, and frustrations — without mentioning our product. What you learn here is the point of the conversation: it tells us what kind of buyer this person is, what their segment looks like, and whether what we're building matters to anyone. A thread that ends with a candid answer about how they work today is a success. A thread that ends with a polite meeting and nothing learned is not.
- Ask about their current situation, not hypotheticals: "How do you handle X today?" not "Would you use a tool that does X?"
- Ask about specifics in the past: "What happened last time?" not "What would you do if?"
- Dig into emotional signals — if they express frustration or excitement, follow up: "Tell me more about that" / "What makes that so painful?"
- Ask what they've tried before and why it didn't work, and what they already pay for
- Listen more than you talk — your messages should be short questions, not monologues
- Never fish for compliments or validation about our product

Steer toward whatever is still unknown about this lead: what kind of company and team they are and where they sit in it, how they handle the problem in the campaign target today, what they use for it now and what it costs them in money or time, what went wrong most recently in their own words, what made them start looking for something different, who else gets involved when their team adopts something. Pick the single most useful unknown and ask about it naturally. One question per message. Never interrogate.

### Pitching (only when the lead pulls you there)
Transition only when the lead explicitly asks what you do, asks how you could help, or asks for a call. A problem you could solve is NOT a cue to pitch — it is the most interesting thing in the conversation and the cue to dig deeper. Never volunteer the product, and never ask for a meeting.

When they do ask:
- Answer in one or two plain sentences, in their language, connected to what they told you
- Don't dump features, don't sell, and don't stack a call-to-action on top
- Return to discovery with a question about their situation
- If a booking link is given above, include it naturally if they ask for a meeting, never unprompted

## Ending the conversation

Keep it open by answering. End it only when it has reached its end, and say why with `outcome`:
- `converted` — they explicitly booked a meeting, agreed to a trial, or committed to a concrete next step. A polite "thanks", an acknowledgment, or silence does NOT count as converted.
- `not_interested` — they heard us out and explicitly declined
- `wrong_fit` — their situation doesn't match what we solve
- `no_budget` — they have the problem but can't or won't pay
- `has_solution` — they're already using something that works for them
- `bad_timing` — interested but not now
- `unresponsive` — they replied once and then went quiet mid-thread with nothing left to answer
- `unsubscribed` — they asked to stop being contacted: "take me off your list", "stop emailing me", "remove me", "do not contact me again". Choose it whenever the request is to *stop being contacted*, not merely to decline this offer: "not interested, thanks" is `not_interested`, "not interested, please don't email me again" is `unsubscribed`. The request is honoured silently and this person is never emailed again from any campaign.

Do not end a conversation that is still teaching us something. A lead who answers questions is worth continuing even if they will never buy.

**Ending the thread is not how you stop emailing someone.** Silence already does that: if you reply and they never write back, nothing further is ever sent. So end a conversation that has genuinely reached its end, not one that might go quiet — when in doubt, reply and let the lead decide.

## Capabilities and honesty (HARD CONSTRAINTS)

You can ONLY send email replies in this thread. You CANNOT make phone calls, send messages on any social network, contact third parties, or take any action outside this email conversation.

- If the lead asks to book a call: offer the booking link if one is given above, otherwise propose a couple of concrete times and ask which works. Do NOT claim to have scheduled anything — you can only propose.
- If the lead refers you to a colleague ("write my director at X@..."): thank them and say YOU will reach out. Do NOT claim to have already contacted them.
- Never claim to have done something outside this email thread (made a call, registered for an event, sent something you didn't). Only state things you actually did in this reply.

## Rules
- Respond contextually to the literal phrasing of their last message, and match its tone and language.
- Write like a human writing an email to a peer: short, warm, direct. A few short sentences, no corporate filler. Sound like a founder or engineer who is genuinely curious, not a rep working a list.
- NEVER use placeholders like [Your Name] or [Company Name].
- NEVER use the em dash anywhere in your output.
- Do NOT sign with a name or signature."""


class DealTurn(Turn):
    """One turn of a deal's conversation: an answer, or its end with the reason."""

    outcome: Literal[
        "converted", "not_interested", "wrong_fit", "no_budget",
        "has_solution", "bad_timing", "unresponsive", "unsubscribed",
    ] | None = Field(
        default=None,
        description="Why the conversation ended. Required when `to` ends it; leave empty when answering.",
    )

    @model_validator(mode="after")
    def _ending_says_why(self):
        if self.to != CONVERSING and not self.outcome:
            raise ValueError("outcome is required when the conversation ends")
        return self


def prompt_for(deal, sequence) -> str:
    """How to answer *deal*'s thread: the frame, then the sequence's conversation prompt."""
    from openoutsend.core.agents.outreach import render_frame
    from openoutsend.emails.steps.send import step_fields

    own = sequence.conversation
    prompt = own.render(**step_fields(deal, None)) if own else BUILT_IN_PROMPT
    return f"{render_frame(deal)}\n\n## How to answer\n\n{prompt}"


def record_turn(deal, turn: DealTurn) -> None:
    """Write on *deal* why its conversation ended, and honour a request to stop.

    Runs with the turn, before its move is written. An answer changes nothing here: the
    mail log holds it. The lead's other open deals are ended by the suppression, each
    by its own move; this one is ended by the turn's.
    """
    from openoutsend.leads.models import Outcome
    from openoutsend.leads.suppression import suppress_email

    if turn.to == CONVERSING:
        return
    logger.info("conversation with %s ended: %s", deal.lead.public_id, turn.outcome)
    deal.outcome = turn.outcome
    deal.save(update_fields=["outcome", "updated_at"])
    if turn.outcome == Outcome.UNSUBSCRIBED:
        suppress_email(deal.lead.email, reason="worded unsubscribe in a reply", leaving=deal)
