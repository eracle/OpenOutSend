# openoutsend/emails/steps/follow_up.py
"""Writing again to somebody who never answered — a nudge on the same thread.

**A follow-up is a feature, not a step.** Its prompt is the shared template
``core/templates/prompts/follow_up.j2`` rather than a file in a sequence folder, and an install turns it on or off (``OUTSEND_FOLLOW_UPS``,
``core/conf.follow_ups_on``). Cold has it on; an event sequence whose steps already say
everything has it off. The planner decides *when* (``planner.awaiting_follow_up``:
business days measured backwards from the mail log, and only if it lands before the
deal's next step is due); this step decides *what*.

**This is cold volume, and it is treated as such.** Unlike a reply, nobody invited it:
it counts against the box's daily cap, waits for the spacing clock, and only leaves
inside the sending window.

**It cannot choose its mailbox.** The thread was opened from one box and answering from
another would break the conversation, so the send goes from `deal.mailbox` or does not
go at all this pass. That is the only structural difference from an opener, which picks
whichever box in the pool is freest.

**It keeps the opener's move without being told it.** The model writes it after the
whole history (``core/agents/history.py``), which includes the prompt the email it
follows was written from — prompt line and all — so a sequence reads as one person
without the line being read back and pasted in a second time.

Why chasing came back, having been deleted: the thing that was actually wrong was
*priority*. Follow-ups outranked openers, open threads accumulate faster than they
close, and a live install measured 102 follow-ups against 1 first email in a week. The
fix there was to delete the feature; the fix here is that both draw from the same
capped, spaced budget, so that ratio is arithmetically unreachable rather than patched
against with a reserved fraction.
"""
from __future__ import annotations

import logging

from termcolor import colored

from openoutsend.core.agents.prompt import render
from openoutsend.leads.models import DealState

logger = logging.getLogger(__name__)

# What every follow-up is written from, rules and all. It takes no variables: what it
# needs about the person comes from the frame and the history.
FOLLOW_UP_PROMPT = render("follow_up.j2")


def send_follow_up(deal) -> DealState | None:
    """Write to *deal* again in its own thread. Returns the next state.

    Returns ``None`` when nothing was sent and the deal should stay where it is —
    a lead suppressed while the agent was writing, or an agent that answered with
    something other than a message.
    """
    from openoutsend.emails.steps.send import send_in_thread

    logger.info("%s %s", colored("▶ follow-up", "cyan", attrs=["bold"]), deal.lead.public_id)
    return send_in_thread(deal, FOLLOW_UP_PROMPT)


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
