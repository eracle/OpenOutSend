# openoutsend/emails/steps/send.py
"""Sending one step of a sequence: the one that opens the thread, or a later one in it.

A prompt step is written by the outreach agent from its prompt and the whole history; an
email step is rendered and sent as written, with no model call. Either way the send is
recorded on the deal, and the deal machine's step that called this moves the deal on
(``leads/lifecycle.py``).

Every step is *cold volume*, which is what a receiver punishes; an answer inside a
thread someone wrote in is not, and goes out with no cap and no spacing (the deal's
conversation, ``machines/``). So each send here rewrites its box's spacing clock
(``space_out``), and the guards only hand over a box that may send (``planner.py``).
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import timedelta

from django.utils import timezone
from termcolor import colored

from openoutsend.core.conf import (
    MIN_SEND_INTERVAL_SECONDS,
    SEND_INTERVAL_JITTER_MAX_SECONDS,
    SEND_INTERVAL_JITTER_MIN_SECONDS,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Plan:
    """What the send guards chose for one send: the box, and the move it opens with.

    ``body`` set means a calling agent wrote the email (``send --agent-draft``), with
    the ``subject`` a thread's first email needs and the rendered ``prompt`` it was
    handed, recorded on the send as the model's would be.
    """

    box: object
    prompt_line: object = None
    subject: str = ""
    body: str = ""
    prompt: str = ""


def send_step(deal, step, plan: Plan) -> bool:
    """Send *step* to *deal* from the planned box. True once it went.

    The deal's first send opens its thread from ``plan.box``; every later one goes in
    that thread, from the thread's own box, under the subject it opened with. False,
    with nothing sent, when the lead was suppressed while the email was being written:
    an unsubscribe can land in the seconds an LLM call takes, and this is the last gate
    before the message goes.

    ``plan.prompt_line`` is the move a prompt step makes (``core/prompt_lines.py``),
    drawn by the pass so two sends in one pass can be told apart. A step whose prompt
    does not name ``{{ prompt_line }}`` drops it, so the log never credits a move the
    model was not shown.
    """
    from openoutsend.emails.sender import suppressed

    opens = deal.thread_id is None
    prompt_line = plan.prompt_line if step.uses_prompt_line else None
    logger.info("%s %s via %s (%s, %s)",
                colored("▶ first email" if opens else "▶ step", "blue", attrs=["bold"]),
                deal.lead.public_id, plan.box.from_address, step.path.name,
                "agent-drafted" if plan.body else
                prompt_line.id if prompt_line else "no prompt line")

    subject, message, prompt = _written(deal, step, plan, prompt_line, opens)
    if suppressed(deal.lead):
        logger.warning("%s was suppressed mid-run — not sending", deal.lead.public_id)
        return False
    if opens:
        _open_thread(deal, plan.box, subject, message, prompt_line, prompt, step)
    else:
        _send_in_thread(deal, message, prompt_line, prompt, step)
    space_out(deal.mailbox, timezone.now())
    return True


def _written(deal, step, plan: Plan, prompt_line, opens: bool) -> tuple[str, str, str]:
    """``(subject, message, prompt)`` of the email to send: drafted, rendered, or written.

    The subject is ``""`` for an email that goes in an existing thread, and the prompt
    is ``""`` for an email step, which was written from none.
    """
    from openoutsend.core.agents.outreach import write_email
    from openoutsend.leads.summaries import materialize_profile_summary_if_missing

    if plan.body:
        return plan.subject, plan.body, plan.prompt
    if step.is_email:
        fields = step_fields(deal, None)
        subject, message = step.opening(**fields) if opens else step.written(**fields)
        return subject, message, ""
    materialize_profile_summary_if_missing(deal.lead)
    prompt = step.render(**step_fields(deal, prompt_line))
    email = write_email(deal, prompt, opener=opens)
    return email.subject or "", email.message, prompt


def _open_thread(deal, mailbox, subject: str, message: str, prompt_line, prompt: str, step) -> None:
    """Send the deal's first email and record the conversation it started.

    The thread the send opens is kept against the deal (``Thread.machine``,
    ``Thread.entity``), so a reply finds the deal waiting for it. It is written once
    the send has gone, never made ahead of it: a refused send is recorded again
    outside the transaction that rolls back here (``sender.Refused``), and must not
    point at a thread that went with it. The deal points at the thread rather than
    copying it: the mail log is the record of what left the box.
    """
    from openoutsend.core.operator import get_active_user
    from openoutsend.emails.sender import operator_bcc, send_email
    from openoutsend.leads.states import machine_name

    sent = send_email(
        mailbox, deal.lead.email, subject, message,
        bcc=operator_bcc(get_active_user()),
        prompt_line=prompt_line,
        prompt=prompt,
        step=step,
        sequence=deal.sequence,
        unsubscribe_url=unsubscribe_url(deal),
    )
    thread = sent.thread
    thread.machine, thread.entity = machine_name(deal.sequence), str(deal.pk)
    thread.save(update_fields=["machine", "entity"])
    deal.mailbox = mailbox
    deal.email_subject = subject
    deal.thread = thread
    deal.email_sent_at = sent.sent_at


def _send_in_thread(deal, message: str, prompt_line, prompt: str, step) -> None:
    """Send *message* in *deal*'s thread under its original subject."""
    from openoutsend.core.operator import get_active_user
    from openoutsend.emails.sender import operator_bcc, send_email
    from openoutsend.emails.threads import message_ids

    chain = message_ids(deal.thread)
    send_email(
        deal.mailbox,
        deal.lead.email,
        # The original subject, unchanged and with no "Re:": a step is ours, whether or
        # not they wrote back. The threading headers are what put it in the same
        # conversation; the subject only has to not lie about it.
        deal.email_subject,
        message,
        bcc=operator_bcc(get_active_user()),
        in_reply_to=chain[-1] if chain else None,
        references=" ".join(chain) or None,
        thread=deal.thread,
        prompt_line=prompt_line,
        prompt=prompt,
        step=step,
        sequence=deal.sequence,
        unsubscribe_url=unsubscribe_url(deal),
    )


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


def unsubscribe_url(deal) -> str:
    """The opt-out link the deal's line was piped with, or "" when it had none.

    A piped field like any other, read by the sender rather than a prompt: a resend
    box puts it in the header and the footer, and a mailbox ignores it.
    """
    return str(deal.fields.get("unsubscribe_url") or "")


def space_out(mailbox, now) -> None:
    """Set when this box may send its next cold email.

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
