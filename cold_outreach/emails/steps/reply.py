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

# `DealState`, the chat-summary update and the suppression writer are the ingest
# model's, and this repo has not built it yet — see
# `roadmap/p1-e2-outsend-ingest-and-packaging.md`. Until it exists these names
# resolve to nothing and this module cannot run.
from openoutreach.crm.models import DealState

logger = logging.getLogger(__name__)


def answer_reply(deal) -> DealState | None:
    """Read the new inbound messages, let the agent decide, execute. Returns next state."""
    from cold_outreach.core.agents.outreach import run_outreach_agent

    logger.info("[%s] %s %s", deal.campaign,
                colored("▶ reply", "green", attrs=["bold"]), deal.lead.profile_url)

    _fold_new_messages_into_summary(deal)
    decision = run_outreach_agent(deal)

    if decision.action == "send_message":
        return _send_reply(deal, decision)
    if decision.action == "mark_completed":
        logger.info("[%s] thread completed for %s: outcome=%s",
                    deal.campaign, deal.lead.profile_url, decision.outcome)
        deal.outcome = decision.outcome
        return DealState.COMPLETED
    if decision.action == "suppress":
        return _suppress(deal)
    return None


def _fold_new_messages_into_summary(deal) -> None:
    """Roll every unanswered inbound turn into ``deal.chat_summary``.

    "Unanswered" is exactly the set that made this deal actionable — inbound turns
    newer than our newest outgoing one — so nothing needs to remember which
    messages have already been folded.

    **Turns only.** A non-delivery report is in the thread and is not one, so it
    can never reach the summary, the agent's context, or a second apology to a
    dead address. That is closed here by the schema rather than by a filter each
    read site has to remember.
    """
    from cold_outreach.core.operator import seller_name
    from cold_outreach.emails.models import Direction
    from openoutreach.core.db.summaries import update_chat_summary

    turns = deal.thread.turns() if deal.thread_id else None
    if turns is None:
        return

    last_outgoing = (
        turns.filter(direction=Direction.OUTBOUND)
        .order_by("-sent_at", "-pk")
        .values_list("sent_at", flat=True)
        .first()
    )
    unanswered = turns.filter(direction=Direction.INBOUND)
    if last_outgoing:
        unanswered = unanswered.filter(sent_at__gt=last_outgoing)
    new_messages = list(unanswered)
    if not new_messages:
        return

    update_chat_summary(deal, new_messages, seller_name=seller_name())
    deal.refresh_from_db(fields=["chat_summary", "profile_summary"])


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
                       deal.campaign, deal.lead.profile_url)
        return None

    logger.info("[%s] reply to %s: %s",
                deal.campaign, deal.lead.profile_url, decision.message)
    chain = _thread_ids(deal)
    send_email(
        deal.mailbox,
        deal.lead.email,
        _reply_subject(deal.email_subject),
        decision.message,
        campaign=deal.campaign,
        bcc=operator_bcc(get_active_user(), deal.campaign),
        in_reply_to=chain[-1] if chain else None,
        references=" ".join(chain) or None,
        thread=deal.thread,
    )
    return None


def _suppress(deal) -> DealState:
    """Honour a worded unsubscribe: suppress the person account-wide, send nothing.

    Enforcement is account-level (``Lead.disqualified``), so it reaches every
    campaign holding the address, not just this thread's. No reply goes out —
    someone who asked to stop hearing from us is not owed one more email.
    """
    from openoutreach.core.db.leads import suppress_email

    suppress_email(deal.lead.email)
    logger.info("[%s] %s asked to stop — suppressed account-wide",
                deal.campaign, deal.lead.profile_url)
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
