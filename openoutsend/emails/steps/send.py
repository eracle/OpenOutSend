# openoutsend/emails/steps/send.py
"""Sending a sequence step — the first, which opens a thread, and every later one in it.

The outreach agent writes the step from its prompt and the whole history, SMTP sends
it, and the send is recorded on the Deal: the first moves it to EMAILED, every one
moves `deal.step`.

Every step is *cold volume*, which is what a receiver punishes; a reply inside a
thread someone started is not, and goes out with no cap and no spacing
(``steps/reply.py``). So each send here rewrites its box's spacing clock
(``space_out``), and the planner only hands over a step whose box may send.
"""
from __future__ import annotations

import logging
import random
from datetime import timedelta

from django.utils import timezone
from termcolor import colored

from openoutsend.core.conf import (
    MIN_SEND_INTERVAL_SECONDS,
    SEND_INTERVAL_JITTER_MAX_SECONDS,
    SEND_INTERVAL_JITTER_MIN_SECONDS,
)

from openoutsend.leads.models import DealState

logger = logging.getLogger(__name__)


def send_first_email(deal, mailbox, step, prompt_line) -> DealState | None:
    """Open the conversation with *deal* from *mailbox* with *step*. Returns the next state.

    *step* is the deal's opening step (``planner.opening_step``) — ``E0`` unless it was
    past its grace when the lead arrived; for cold, ``E0-opener.md``. The caller has
    already established that this box may send right now (``planner.free_for_first_email``).
    Returns ``None`` without sending if the lead was suppressed while the agent was
    writing — an unsubscribe can land in the seconds an LLM call takes, and this is the
    last gate before the message goes.

    ``prompt_line`` is the move this opener makes (``core/prompt_lines.py``), and it is
    required — an opener that reaches the log unattributed is a hole nothing can fill
    in afterwards, and a default would let a new call site open one silently. ``None``
    is a legitimate value (an install with no prompt lines), but it has to be said.
    The **pass** draws it rather than this function, so two sends in one pass can be
    told apart instead of sharing whatever one call happened to pick. A step whose
    prompt does not name ``{{ prompt_line }}`` drops it, so the log never credits a
    move the model was not shown.
    """
    from openoutsend.core.agents.outreach import run_outreach_agent
    from openoutsend.emails.sender import suppressed
    from openoutsend.leads.summaries import materialize_profile_summary_if_missing

    if not step.uses_prompt_line:
        prompt_line = None
    logger.info("%s %s via %s (%s, %s)",
                colored("▶ first email", "blue", attrs=["bold"]),
                deal.lead.public_id, mailbox.from_address, step.path.name,
                prompt_line.id if prompt_line else "no prompt line")

    materialize_profile_summary_if_missing(deal.lead)
    prompt = step.render(**step_fields(deal, prompt_line))
    opener = run_outreach_agent(deal, prompt, opener=True)

    if suppressed(deal.lead):
        logger.warning("%s was suppressed mid-run — not sending", deal.lead.public_id)
        return None

    return _finish_send(deal, mailbox, opener.subject, opener.message, prompt_line,
                        prompt=prompt, step=step)


def send_drafted_email(deal, mailbox, step, subject: str, message: str,
                       prompt_line) -> DealState | None:
    """Open the conversation with a subject/message an agent wrote, not `AI_MODEL`.

    The mirror image of `send_first_email` with the LLM call removed — everything
    after it (the suppression recheck, the SMTP send, the deal bookkeeping, the
    spacing clock) is identical, because none of that cares who wrote the words.
    It is still the deal's opening *step*, and recorded as one; there is no rendered
    prompt to record, because the calling agent wrote from its own context.
    """
    from openoutsend.emails.sender import suppressed

    logger.info("%s %s via %s (agent-drafted)",
                colored("▶ first email", "blue", attrs=["bold"]),
                deal.lead.public_id, mailbox.from_address)

    if suppressed(deal.lead):
        logger.warning("%s was suppressed while its draft waited — not sending",
                       deal.lead.public_id)
        return None

    return _finish_send(deal, mailbox, subject, message, prompt_line,
                        prompt="", step=step)


def send_next_step(deal, step, prompt_line) -> DealState | None:
    """Write *step* to *deal* in its own thread, from its own box. Returns the next state.

    The planner has established the step is due and the thread's box may send
    (``planner.cold_sends_due``). Same thread and no new subject: the reader sees one
    conversation, and the step's prompt says what this email is for. ``prompt_line``
    follows ``send_first_email``'s rule and is dropped by a step that does not name it.
    """
    from openoutsend.leads.summaries import materialize_profile_summary_if_missing

    if not step.uses_prompt_line:
        prompt_line = None
    logger.info("%s %s via %s (%s)", colored("▶ step", "blue", attrs=["bold"]),
                deal.lead.public_id, deal.mailbox.from_address, step.path.name)

    materialize_profile_summary_if_missing(deal.lead)
    prompt = step.render(**step_fields(deal, prompt_line))
    return send_in_thread(deal, prompt, step=step, prompt_line=prompt_line)


def send_in_thread(deal, prompt: str, *, step=None, prompt_line=None) -> DealState | None:
    """Have the agent write *prompt* in *deal*'s thread, and send what it wrote.

    The shared tail of a later step and a follow-up — every send we make into a thread
    nobody has answered. Returns ``None`` when the deal stays where it is: sent, or
    nothing sent because the lead was suppressed while the agent wrote, or the agent
    answered with something other than a message.

    *step* is recorded on the send and moves ``deal.step``; a follow-up has none.
    """
    from openoutsend.core.agents.outreach import run_outreach_agent
    from openoutsend.core.operator import get_active_user
    from openoutsend.emails.sender import operator_bcc, send_email, suppressed
    from openoutsend.emails.steps.reply import ends_in_an_opt_out, honour_opt_out, thread_ids

    decision = run_outreach_agent(deal, prompt)

    # Written to somebody who has said nothing, so there is no opt-out here to read
    # and no prompt offers one. It is honoured anyway if the agent reaches for it: the
    # alternative is the branch below, which logs the decision and discards it —
    # leaving a deal the agent judged an opt-out sendable again next pass.
    if ends_in_an_opt_out(decision):
        return honour_opt_out(deal)
    if decision.action == "mark_completed":
        logger.info("%s judged not worth another email: %s",
                    deal.lead.public_id, decision.outcome)
        deal.outcome = decision.outcome
        return DealState.COMPLETED
    if decision.action != "send_message":
        logger.warning("agent for %s answered %s in thread — sending nothing",
                       deal.lead.public_id, decision.action)
        return None

    # Last gate before the message goes, exactly as the opener has: an opt-out can
    # land in the seconds an LLM call takes, and a bounce may have suppressed the
    # address since the pool was built.
    if suppressed(deal.lead):
        logger.warning("%s was suppressed mid-run — not sending", deal.lead.public_id)
        return None

    chain = thread_ids(deal)
    send_email(
        deal.mailbox,
        deal.lead.email,
        # The original subject, unchanged and with no "Re:" — they never wrote back,
        # so there is nothing to be replying to. The threading headers are what put
        # it in the same conversation; the subject only has to not lie about it.
        deal.email_subject,
        decision.message,
        bcc=operator_bcc(get_active_user()),
        in_reply_to=chain[-1] if chain else None,
        references=" ".join(chain) or None,
        thread=deal.thread,
        prompt_line=prompt_line,
        prompt=prompt,
        step=step,
        sequence=deal.sequence,
    )
    if step is not None:
        deal.step = step.number
    space_out(deal.mailbox, timezone.now())
    return None


def step_fields(deal, prompt_line) -> dict:
    """What a step file's ``{{ field }}`` can name: the lead's record, the fields piped
    for this sequence over it, and the drawn move.

    ``prompt_line`` is always defined — ``""`` when none was drawn — so a step can test
    ``{% if prompt_line %}`` under ``StrictUndefined``.
    """
    lead = deal.lead
    return {
        "first_name": lead.first_name,
        "last_name": lead.last_name,
        "company": lead.company,
        "title": lead.title,
        "website": lead.website,
        "linkedin_url": lead.linkedin_url,
        "email": lead.email,
        **deal.fields,
        "prompt_line": prompt_line.prompt if prompt_line else "",
    }


def _finish_send(deal, mailbox, subject: str, message: str, prompt_line, *,
                 prompt: str, step) -> DealState:
    """SMTP-send the opener and record it on *deal*. Shared by both drafting paths."""
    from openoutsend.core.operator import get_active_user
    from openoutsend.emails.sender import operator_bcc, send_email

    sent = send_email(
        mailbox, deal.lead.email, subject, message,
        bcc=operator_bcc(get_active_user()),
        prompt_line=prompt_line,
        prompt=prompt,
        step=step,
        sequence=deal.sequence,
    )

    # The send wrote itself into the mail log and opened a thread; the deal just
    # points at the conversation it started. Nothing about this message is stored
    # twice, so nothing about it can disagree with itself later.
    deal.mailbox = mailbox
    deal.email_subject = subject
    deal.thread = sent.thread
    deal.email_sent_at = sent.sent_at
    deal.step = step.number
    space_out(mailbox, timezone.now())
    return DealState.EMAILED


def space_out(mailbox, now) -> None:
    """Set when this box may send its next cold email — an opener or a follow-up.

    Fresh jitter every time: a fixed cadence is its own machine signature, so the
    gap has to vary. Per box, because the daily ceiling is per box — two mailboxes
    are two sending identities and one receiver's rhythm says nothing about the
    other's.

    A time past the end of the working day is written unchanged rather than
    clamped to tomorrow morning: the window is enforced pool-wide in
    ``planner.free_for_first_email``, and a second clamp here would be a
    copy of that rule free to drift from it.
    """
    mailbox.next_send_at = now + timedelta(
        seconds=MIN_SEND_INTERVAL_SECONDS + random.uniform(
            SEND_INTERVAL_JITTER_MIN_SECONDS, SEND_INTERVAL_JITTER_MAX_SECONDS),
    )
    mailbox.save(update_fields=["next_send_at"])
