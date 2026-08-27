# cold_outreach/emails/steps/reply.py
"""Answering someone who wrote back — the only outbound mail after the first email.

This step runs on a deal whose newest inbound message is newer than its newest
outgoing one, so there is always something to answer and the agent is never asked
to decide about silence. That is the whole follow-up policy: **a lead who does not
reply is never emailed again.**

What that deletes is worth naming, because it was the most intricate machinery in
the old scheduler. There was a per-deal countdown (``next_follow_up_at``) that the
agent itself re-armed in business hours; a claim priority putting follow-ups above
first emails; and — because that priority is not ownership — a floor
(``OPENER_FLOOR_FRACTION``) reserving a quarter of the mailbox for first contact,
capped in turn by the openers actually ready to send so nothing idled. All of it
existed because open threads accumulate faster than they close, so an unbounded
follow-up drain eventually owned every send in the box: measured on a live install,
102 follow-ups and 1 first email in a week. None of it is needed once a reply is
the trigger — replies are bounded by how many people write back, and they are not
cold volume, so they compete for nothing.

Sending here is therefore **exempt from the daily cap and from send spacing**.
Answering within minutes of being written to is more human, not less.
"""
from __future__ import annotations

import logging

from termcolor import colored

from cold_outreach.leads.models import DealState

logger = logging.getLogger(__name__)


def answer_reply(deal) -> DealState | None:
    """Read the new inbound messages, let the agent decide, execute. Returns next state."""
    from cold_outreach.core.agents.outreach import run_outreach_agent

    logger.info("[%s] %s %s", deal.campaign,
                colored("▶ reply", "green", attrs=["bold"]), deal.lead.public_id)

    _fold_new_messages_into_summary(deal)
    decision = run_outreach_agent(deal)

    if decision.action == "send_message":
        return _send_reply(deal, decision)
    if decision.action == "mark_completed":
        logger.info("[%s] thread completed for %s: outcome=%s",
                    deal.campaign, deal.lead.public_id, decision.outcome)
        deal.outcome = decision.outcome
        return DealState.COMPLETED
    if decision.action == "suppress":
        return _suppress(deal)
    return None


def _fold_new_messages_into_summary(deal) -> None:
    """Roll every unanswered inbound turn into ``deal.chat_summary``.

    "Unanswered" is exactly the set that made this deal actionable — inbound turns
    newer than our newest outgoing one — so nothing needs to remember which messages
    have already been folded.

    **Turns only.** A non-delivery report is in the thread and is not one, so it can
    never reach the summary, the agent's context, or a second apology to a dead
    address. That is closed here by the schema rather than by a filter each read site
    has to remember.
    """
    from cold_outreach.core.operator import seller_name
    from cold_outreach.emails.models import Direction
    from cold_outreach.leads.summaries import update_chat_summary

    if not deal.thread_id:
        return

    turns = deal.thread.turns()
    last_outgoing = (
        turns.filter(direction=Direction.OUTBOUND)
        .order_by("-sent_at", "-pk")
        .values_list("sent_at", flat=True)
        .first()
    )
    unanswered = turns.filter(direction=Direction.INBOUND)
    if last_outgoing:
        unanswered = unanswered.filter(sent_at__gt=last_outgoing)

    update_chat_summary(deal, list(unanswered), seller_name=seller_name())


# ── Decision execution ────────────────────────────────────────────


def _send_reply(deal, decision) -> DealState | None:
    """Send a threaded reply and record it. The deal stays EMAILED.

    No state change and no timer: writing the outgoing message is itself what makes
    the deal stop being actionable, because its newest message is ours again.
    """
    from cold_outreach.core.operator import get_active_user
    from cold_outreach.emails.sender import operator_bcc, send_email, suppressed

    if suppressed(deal.lead):
        logger.warning("[%s] %s was suppressed mid-run — not replying",
                       deal.campaign, deal.lead.public_id)
        return None

    logger.info("[%s] reply to %s: %s",
                deal.campaign, deal.lead.public_id, decision.message)
    chain = _thread_ids(deal)
    send_email(
        deal.mailbox,
        deal.lead.email,
        _reply_subject(deal.email_subject),
        decision.message,
        bcc=operator_bcc(get_active_user()),
        in_reply_to=chain[-1] if chain else None,
        references=" ".join(chain) or None,
        thread=deal.thread,
        # A reply answers what they wrote. There is no move picked in advance to
        # attribute it to, and inventing one would put noise in the log the opener
        # comparison reads.
        prompt_line=None,
    )
    return None


def _suppress(deal) -> DealState:
    """Honour a worded unsubscribe: suppress the address for good, send nothing.

    Enforcement is address-level, so it reaches every campaign holding that address,
    not just this thread's — and it is terminal: no later ingest can walk this person
    back into the sendable set. No reply goes out either; someone who asked to stop
    hearing from us is not owed one more email.
    """
    from cold_outreach.leads.suppression import suppress_email

    suppress_email(deal.lead.email, reason="worded unsubscribe in a reply")
    logger.info("[%s] %s asked to stop — suppressed for good",
                deal.campaign, deal.lead.public_id)
    return DealState.UNSUBSCRIBED


# ── Helpers ───────────────────────────────────────────────────────


def _reply_subject(opener_subject: str) -> str:
    """``Re:`` the opener's subject, without stacking a second ``Re:``."""
    subject = opener_subject or ""
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


def _thread_ids(deal) -> list[str]:
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
