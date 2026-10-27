# openoutsend/emails/steps/reply.py
"""Answering someone who wrote back.

This step runs on a deal whose newest inbound message is newer than its newest
outgoing one, so there is always something to answer and the agent is never asked to
decide about silence. **Silence is `emails/steps/follow_up.py`'s question, not this
one** — a lead who says nothing gets at most two more cold emails and then the pursuit
ends; a lead who answers is here, and is never chased again.

Sending here is **exempt from the daily cap, the send spacing and the sending
window** — the only outbound mail that is. Answering within minutes of being written
to is more human, not less, and a reply is not cold volume: it is bounded by how many
people write back, so it competes for nothing.

**A follow-up is the opposite on every count**, and the reason is the machinery this
rewrite deleted. The old scheduler had a per-deal countdown (``next_follow_up_at``)
the agent re-armed itself, a claim priority putting follow-ups *above* first emails,
and — because that priority is not ownership — a floor (``OPENER_FLOOR_FRACTION``)
reserving a quarter of the mailbox for first contact. All of it existed because open
threads accumulate faster than they close, so an unbounded follow-up drain eventually
owned every send in the box: measured on a live install, 102 follow-ups and 1 first
email in a week. Chasing came back without any of it — no countdown on a row, no
separate budget, no reserved fraction — because follow-ups now draw from the same
capped and spaced pool as openers, which makes that ratio arithmetically unreachable
rather than something a floor has to defend against.
"""
from __future__ import annotations

import logging

from termcolor import colored

from openoutsend.leads.models import DealState, Outcome

logger = logging.getLogger(__name__)

# What the agent is asked when somebody wrote back. Hardcoded here, beside the step that
# sends it, like the follow-up's; the history above it carries their message and every
# prompt and email before it, so it says only how to answer.
REPLY_PROMPT = """\
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

## Actions

Choose exactly one:

- **send_message**: Send a short email reply in this thread. No `subject`; the thread keeps its own.
- **suppress**: The lead asked to stop being contacted — "stop", "unsubscribe", "take me off your list", "stop emailing me", "remove me", "do not contact me again". Every email we send ends with a line inviting exactly this, so a one-word reply asking for it is a deliberate answer, not a curt one. End the thread and never email this person again from any campaign. Choose this over `mark_completed` whenever the request is to *stop being contacted*, not merely to decline this offer: "not interested, thanks" is `mark_completed` with `not_interested`, "not interested, please don't email me again" is `suppress`. Send no reply — the request is honoured silently, and the conversation is recorded with the `unsubscribed` outcome for you.
- **mark_completed**: End the conversation. You must choose an `outcome`:
  - `converted` — they explicitly booked a meeting, agreed to a trial, or committed to a concrete next step. A polite "thanks", an acknowledgment, or silence does NOT count as converted.
  - `not_interested` — they heard us out and explicitly declined
  - `wrong_fit` — their situation doesn't match what we solve
  - `no_budget` — they have the problem but can't or won't pay
  - `has_solution` — they're already using something that works for them
  - `bad_timing` — interested but not now
  - `unresponsive` — they replied once and then went quiet mid-thread with nothing left to answer
  - `unsubscribed` — they asked to stop being contacted. Reach for `suppress` instead, which records this outcome and puts the address beyond every campaign; naming it here means the same thing and is honoured the same way, so never pair it with a reply you expect to be sent.

Do not end a conversation that is still teaching us something. A lead who answers questions is worth continuing even if they will never buy.

**Ending the thread is not how you stop emailing someone.** Silence already does that: if you reply and they never write back, nothing further is ever sent. So `mark_completed` is for a conversation that has genuinely reached its end, not for one that might go quiet — when in doubt, reply and let the lead decide.

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


def answer_reply(deal) -> DealState | None:
    """Let the agent read the whole history and decide, then execute. Returns next state."""
    from openoutsend.core.agents.outreach import run_outreach_agent

    logger.info("%s %s", colored("▶ reply", "green", attrs=["bold"]), deal.lead.public_id)

    decision = run_outreach_agent(deal, REPLY_PROMPT)

    if ends_in_an_opt_out(decision):
        return honour_opt_out(deal)
    if decision.action == "send_message":
        return _send_reply(deal, decision.message)
    if decision.action == "mark_completed":
        logger.info("thread completed for %s: outcome=%s",
                    deal.lead.public_id, decision.outcome)
        deal.outcome = decision.outcome
        return DealState.COMPLETED
    return None


def ends_in_an_opt_out(decision) -> bool:
    """True when the agent said this person asked to stop — in either field it can say it.

    The action and the outcome are one sentence in two places: `suppress` is the move,
    `unsubscribed` is the reason it records. Asked first, and before the action is
    branched on, because the two must not be able to disagree — a decision naming the
    outcome while reaching for `mark_completed` would otherwise close the deal and
    leave the address off the list, which is the one failure an opt-out cannot have.
    """
    return decision.action == "suppress" or decision.outcome == Outcome.UNSUBSCRIBED


def send_drafted_reply(deal, body: str) -> DealState | None:
    """Send a reply a calling agent wrote (``send --agent-draft``). Returns next state.

    The agent writes the answer only: ending the thread or honouring a worded
    unsubscribe stays a choice the model path makes, or the operator.
    """
    logger.info("%s %s", colored("▶ reply (drafted)", "green", attrs=["bold"]),
                deal.lead.public_id)
    return _send_reply(deal, body)


# ── Decision execution ────────────────────────────────────────────


def _send_reply(deal, message: str) -> DealState | None:
    """Send a threaded reply and record it. The deal stays EMAILED.

    No state change and no timer: writing the outgoing message is itself what makes
    the deal stop being actionable, because its newest message is ours again.
    """
    from openoutsend.core.operator import get_active_user
    from openoutsend.emails.sender import operator_bcc, send_email, suppressed

    if suppressed(deal.lead):
        logger.warning("%s was suppressed mid-run — not replying", deal.lead.public_id)
        return None

    logger.info("reply to %s: %s", deal.lead.public_id, message)
    chain = thread_ids(deal)
    send_email(
        deal.mailbox,
        deal.lead.email,
        _reply_subject(deal.email_subject),
        message,
        bcc=operator_bcc(get_active_user()),
        in_reply_to=chain[-1] if chain else None,
        references=" ".join(chain) or None,
        thread=deal.thread,
        # A reply answers what they wrote. There is no move picked in advance to
        # attribute it to, and inventing one would put noise in the log the opener
        # comparison reads.
        prompt_line=None,
        prompt=REPLY_PROMPT,
        sequence=deal.sequence,
    )
    return None


def honour_opt_out(deal) -> DealState:
    """Honour a worded unsubscribe: suppress the address for good, send nothing.

    Enforcement is address-level and terminal: no later ingest can walk this person
    back into the sendable set. No reply goes out either; someone who asked to stop
    hearing from us is not owed one more email.

    The deal closes like every other ended conversation — `COMPLETED`, with the
    outcome saying why. The address being on the list is the part that is terminal;
    the deal is just a deal that ended.
    """
    from openoutsend.leads.suppression import suppress_email

    suppress_email(deal.lead.email, reason="worded unsubscribe in a reply")
    logger.info("%s asked to stop — suppressed for good", deal.lead.public_id)
    deal.outcome = Outcome.UNSUBSCRIBED
    return DealState.COMPLETED


# ── Helpers ───────────────────────────────────────────────────────


def _reply_subject(opener_subject: str) -> str:
    """``Re:`` the opener's subject, without stacking a second ``Re:``."""
    subject = opener_subject or ""
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


def thread_ids(deal) -> list[str]:
    """Every Message-ID in this conversation, oldest first, bracketed for a header.

    The whole chain, because that is what ``References`` is for: a client that
    threads on the root and one that threads on the newest message both find their
    anchor in it, and the last entry is the parent ``In-Reply-To`` names.
    """
    if not deal.thread_id:
        return []
    return [
        f"<{message_id}>"
        for message_id in deal.thread.messages.order_by("sent_at", "pk")
        .values_list("message_id", flat=True)
        if not message_id.startswith("sha256:")
    ]
