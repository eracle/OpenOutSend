# cold_outreach/emails/steps/send.py
"""The first email — the only cold message this person will ever get.

The outreach agent opens the conversation, SMTP sends it, and the send is recorded
on the Deal, which moves it to EMAILED. From there the deal is only ever touched
again if the recipient answers.

That is the whole reason this step is the one under a cap. A first email is *cold
volume*, which is what a receiver punishes; a reply inside a thread someone started
is not, and goes out with no cap and no spacing (``steps/reply.py``). So the two
guards — the box's measured daily ceiling and the ≥3-minute spacing — live here and
nowhere else.
"""
from __future__ import annotations

import logging
import random
from datetime import timedelta

from django.utils import timezone
from termcolor import colored

from cold_outreach.core.conf import (
    MIN_SEND_INTERVAL_SECONDS,
    SEND_INTERVAL_JITTER_MAX_SECONDS,
    SEND_INTERVAL_JITTER_MIN_SECONDS,
)

from cold_outreach.leads.models import DealState

logger = logging.getLogger(__name__)


def send_first_email(deal, mailbox, prompt_line) -> DealState | None:
    """Open the conversation with *deal* from *mailbox*. Returns the next state.

    The caller has already established that this box may send right now
    (``Mailbox.objects.free_for_first_email``). Returns ``None`` without sending if
    the lead was suppressed while the agent was writing — an unsubscribe can land in
    the seconds an LLM call takes, and this is the last gate before the message goes.

    ``prompt_line`` is the move this opener makes (``core/prompt_lines.py``), and it is
    required — an opener that reaches the log unattributed is a hole nothing can fill
    in afterwards, and a default would let a new call site open one silently. ``None``
    is a legitimate value (an install with no prompt lines), but it has to be said.
    The **pass** draws it rather than this function, so two sends in one pass can be
    told apart instead of sharing whatever one call happened to pick.
    """
    from cold_outreach.core.agents.outreach import run_outreach_agent
    from cold_outreach.core.operator import get_active_user
    from cold_outreach.emails.sender import operator_bcc, send_email, suppressed
    from cold_outreach.leads.summaries import materialize_profile_summary_if_missing

    logger.info("[%s] %s %s via %s (%s)", deal.campaign,
                colored("▶ first email", "blue", attrs=["bold"]),
                deal.lead.public_id, mailbox.from_address,
                prompt_line.id if prompt_line else "no prompt line")

    materialize_profile_summary_if_missing(deal.lead)
    opener = run_outreach_agent(deal, prompt_line)

    if suppressed(deal.lead):
        logger.warning("[%s] %s was suppressed mid-run — not sending",
                       deal.campaign, deal.lead.public_id)
        return None

    sent = send_email(
        mailbox, deal.lead.email, opener.subject, opener.message,
        bcc=operator_bcc(get_active_user()),
        prompt_line=prompt_line,
    )

    # The send wrote itself into the mail log and opened a thread; the deal just
    # points at the conversation it started. Nothing about this message is stored
    # twice, so nothing about it can disagree with itself later.
    deal.mailbox = mailbox
    deal.email_subject = opener.subject
    deal.thread = sent.thread
    deal.email_sent_at = sent.sent_at
    _space_out(mailbox, timezone.now())
    return DealState.EMAILED


def _space_out(mailbox, now) -> None:
    """Set when this box may send its next first email.

    Fresh jitter every time: a fixed cadence is its own machine signature, so the
    gap has to vary. Per box, because the daily ceiling is per box — two mailboxes
    are two sending identities and one receiver's rhythm says nothing about the
    other's.

    A time past the end of the working day is written unchanged rather than
    clamped to tomorrow morning: the window is enforced pool-wide in
    ``Mailbox.objects.free_for_first_email``, and a second clamp here would be a
    copy of that rule free to drift from it.
    """
    mailbox.next_send_at = now + timedelta(
        seconds=MIN_SEND_INTERVAL_SECONDS + random.uniform(
            SEND_INTERVAL_JITTER_MIN_SECONDS, SEND_INTERVAL_JITTER_MAX_SECONDS),
    )
    mailbox.save(update_fields=["next_send_at"])
