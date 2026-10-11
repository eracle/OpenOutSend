# openoutsend/emails/steps/follow_up.py
"""Writing again to somebody who never answered — a nudge on the same thread.

**A follow-up is a feature, not a step.** Its prompt is hardcoded here rather than in
a sequence folder, and an install turns it on or off (``OUTSEND_FOLLOW_UPS``,
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

from openoutsend.leads.models import DealState

logger = logging.getLogger(__name__)

FOLLOW_UP_PROMPT = """\
They have not answered your last email. They have said nothing at all — not "no", not "later", nothing. Assume it was simply missed.

Write one more short email in the same thread: a `message` and no `subject`, since it keeps the original. Or, if the facts about them say this person is plainly the wrong fit, choose `mark_completed` with `wrong_fit` instead of writing.

**Say something new.** A follow-up whose whole content is that a previous email exists is the thing everyone deletes. No "just bumping this", no "did you see my last email", no "following up on the below", no apology for writing again. If you have nothing to add, ask a different and smaller question than last time.

**Never imply obligation.** They did not owe you an answer and still do not. No "I did not hear back", no "I will assume you are not interested", no counting of how many times you have written.

**Shorter than the last one.** Each touch has less claim on their attention than the one before it.

**Stay in the voice of the email it follows.** Its prompt is above: keep the move it made. This is one person writing twice, not two campaigns.

### Rules this email cannot break

These override anything above.

- **At most 75 words.** Write a short message; do not write a long one and squeeze it.
- **No link of any kind**, and **no meeting request**: one question they can answer in a sentence from their phone.
- **Only claims the facts about them can source.** If it is not in the record, you do not know it.
- **No machine tells.** No em dash, no three-item lists, no two perfectly balanced clauses in a row.
- Write in the same language as your last email, whatever the facts about them suggest. Switching languages mid-thread reads as a different sender.
- No placeholders like [Your Name], and no signature."""


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
