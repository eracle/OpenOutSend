# cold_outreach/emails/steps/follow_up.py
"""Writing again to somebody who never answered — the second and third touch.

**This is cold volume, and it is treated as such.** Unlike a reply, nobody invited it:
it counts against the box's daily cap, waits for the spacing clock, and only leaves
inside the sending window. The pool decides *when* (`leads/pools.awaiting_follow_up`,
business days measured backwards from the mail log); this step decides *what*.

**It cannot choose its mailbox.** The thread was opened from one box and answering from
another would break the conversation, so the send goes from `deal.mailbox` or does not
go at all this pass. That is the only structural difference from an opener, which picks
whichever box in the pool is freest.

**It reuses the opener's prompt line**, read back off the first outbound message in the
thread. Two reasons, and the second matters more: a sequence written from one move reads
as one person, and a reply that lands on touch three can be attributed to a single line
rather than to a mixture of them.

Why chasing came back, having been deleted: the thing that was actually wrong was
*priority*. Follow-ups outranked openers, open threads accumulate faster than they
close, and a live install measured 102 follow-ups against 1 first email in a week. The
fix there was to delete the feature; the fix here is that both draw from the same
capped, spaced budget, so that ratio is arithmetically unreachable rather than patched
against with a reserved fraction.
"""
from __future__ import annotations

import logging

from django.utils import timezone
from termcolor import colored

from cold_outreach.leads.models import DealState

logger = logging.getLogger(__name__)


def send_follow_up(deal) -> DealState | None:
    """Write to *deal* again in its own thread. Returns the next state.

    Returns ``None`` when nothing was sent and the deal should stay where it is —
    a lead suppressed while the agent was writing, or an agent that answered with
    something other than a message.
    """
    from cold_outreach.core.agents.outreach import FOLLOW_UP, run_outreach_agent
    from cold_outreach.core.operator import get_active_user
    from cold_outreach.emails.sender import operator_bcc, send_email, suppressed
    from cold_outreach.emails.steps.reply import ends_in_an_opt_out, honour_opt_out, thread_ids
    from cold_outreach.emails.steps.send import space_out

    prompt_line = opening_prompt_line(deal)
    logger.info("%s %s (%s)", colored("▶ follow-up", "cyan", attrs=["bold"]),
                deal.lead.public_id, prompt_line.id if prompt_line else "no prompt line")

    decision = run_outreach_agent(deal, prompt_line, stage=FOLLOW_UP)

    # A chase is written to somebody who has said nothing, so there is no opt-out here
    # to read and the prompt does not offer one. It is honoured anyway if the agent
    # reaches for it: the alternative is the branch below, which logs the decision and
    # discards it — leaving a deal the agent judged an opt-out sendable again next pass.
    if ends_in_an_opt_out(decision):
        return honour_opt_out(deal)
    if decision.action == "mark_completed":
        logger.info("%s judged not worth another email: %s",
                    deal.lead.public_id, decision.outcome)
        deal.outcome = decision.outcome
        return DealState.COMPLETED
    if decision.action != "send_message":
        logger.warning("follow-up agent for %s answered %s — sending nothing",
                       deal.lead.public_id, decision.action)
        return None

    # Last gate before the message goes, exactly as the opener has: an opt-out can
    # land in the seconds an LLM call takes, and a bounce may have suppressed the
    # address since the pool was built.
    if suppressed(deal.lead):
        logger.warning("%s was suppressed mid-run — not following up", deal.lead.public_id)
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
    )
    space_out(deal.mailbox, timezone.now())
    return None


def give_up(deal) -> DealState:
    """End a pursuit nobody ever answered. The address is not suppressed.

    `unresponsive` already exists for the lead who replies once and goes quiet; this
    is the same fact one touch earlier. **Deliberately not a suppression** — silence is
    not a request to stop, and putting a silent lead on the list would forbid a later
    approach that might actually suit them.
    """
    logger.info("%s never answered — ending the pursuit", deal.lead.public_id)
    deal.outcome = "unresponsive"
    return DealState.COMPLETED


def opening_prompt_line(deal):
    """The prompt line the opener was written from, or ``None``.

    Read back off the thread rather than carried on the deal: the mail log already
    records it against the send, and a second copy on the deal is a second thing that
    can disagree. ``None`` covers a thread opened before prompt lines existed and an
    install with none, both of which still get a follow-up — just an unattributed one.
    """
    from cold_outreach.core.prompt_lines import PromptLineError, choose
    from cold_outreach.emails.models import Direction

    if not deal.thread_id:
        return None
    opener = (
        deal.thread.messages
        .filter(direction=Direction.OUTBOUND)
        .exclude(prompt_line_id="")
        .order_by("sent_at", "pk")
        .first()
    )
    if opener is None:
        return None
    try:
        return choose(opener.prompt_line_id)
    except PromptLineError:
        # The line was renamed or deleted after the opener went out. The follow-up is
        # worth more than the attribution, so it goes without one.
        logger.warning("prompt line %r is gone — following up %s without it",
                       opener.prompt_line_id, deal.lead.public_id)
        return None


