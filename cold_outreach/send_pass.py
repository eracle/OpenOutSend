"""One pass: read the mail, answer what came back, open what there is room to open.

**Bounded, like the finder's `find`.** It does what the guards allow *right now* and
exits — no daemon, no per-recipient timers, no loop waiting for a clock. Cadence is a
timer's job, and the cadence a mailbox needs is minutes, not residency. This project's
parent ran the other experiment: it had a daemon, deleted it, and replaced it with one
bounded verb behind a systemd timer.

The order is the design, and each step feeds the next:

    read      IMAP → rows → kinds → events, suppression   (`emails/mail_pass.py`)
    answer    every thread the lead has replied in         (no cap, no spacing)
    open      up to `goal` first emails, spaced             (capped, spaced, in-window)

Reading first is what makes the other two honest: an opt-out that arrived overnight
suppresses the person *before* anything is written to them, and a reply is visible in
the pass that answers it.

**Openers are the only cold volume**, so they are the only thing under a cap. A reply
inside a thread somebody started is not cold volume, and answering within minutes is
more human, not less — so replies ignore the daily ceiling, the spacing clock and the
sending window alike.

**`goal` is the finder's `job.py` lesson applied here, not a daemon reintroduced.**
Without it, a pass opens at most one conversation per box and stops — the per-box
spacing clock (~3.5–4.5 min, `core/conf.py`) takes the box "not free" again before a
second send could land in the same call. That gap is a *short* bound, the same class
as the finder's provider-retry backoff, and just as safe to sleep through inside one
process (`_wait_for_spacing`). The sending window (hours) and the daily headroom
(until tomorrow) are not — sleeping through *those* is exactly the "residency" this
module's docstring already rejects for the daemon it replaced. So a goal-bounded pass
loops and sleeps only on spacing, and stops the moment the wall it hits is the window
or the headroom, reporting how far it got and why — the same shape as the finder's
`goal_unreached`. For a goal inside one day's headroom this needs no external timer at
all; for anything past it, the next invocation (however it is triggered) picks up
where this one stopped, because nothing here is state a lost process would strand.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class PassResult:
    """What one pass did. The counts are what `outsend send` prints."""

    mirrored: int = 0
    classified: int = 0
    projected: int = 0
    answered: int = 0
    opened: int = 0
    followed_up: int = 0
    gave_up: int = 0
    failed: int = 0

    @property
    def ok(self) -> bool:
        """True when nothing raised on its way out."""
        return self.failed == 0


def run_send_pass(campaign, prompt_line_name: str | None = None, goal: int | None = None) -> PassResult:
    """Read, answer, open — and report. Never raises for a failed send.

    ``prompt_line_name`` pins every opener in this pass to one move
    (``core/prompt_lines.py``); left unset, each opener draws its own at random, which
    is what makes the log a comparison rather than a run of whatever was pinned.

    ``goal`` is how many first emails *this call* should open — a delta, in the
    finder's `Goal` sense, not a running total to reach. Left unset, the pass opens
    at most one per free box and returns, exactly as before this existed. Given a
    number, it keeps opening — sleeping through the spacing clock between sends —
    until ``goal`` is met or a wall it cannot sleep through (the window, the day's
    headroom) is the thing actually holding it up.
    """
    from cold_outreach.emails.mail_pass import run_mail_pass

    result = PassResult()
    result.mirrored, result.classified, result.projected = run_mail_pass()
    _answer_replies(campaign, result)
    _follow_up(campaign, result)
    _open_conversations(campaign, result, prompt_line_name, goal)
    logger.info("%s", _what_is_holding(campaign))
    return result


def _answer_replies(campaign, result: PassResult) -> None:
    """Answer every thread whose newest turn is theirs.

    The pool is materialised before the loop rather than re-queried: a deal whose
    reply *fails* to send keeps its state, so asking the database again would hand
    back the same row forever. A failure costs that one conversation this pass, and
    the next pass tries it again.
    """
    from cold_outreach.emails.steps.reply import answer_reply
    from cold_outreach.leads.pools import unanswered_replies

    for deal in list(unanswered_replies(campaign)):
        try:
            _apply(deal, answer_reply(deal))
            result.answered += 1
        except Exception:
            logger.exception("reply to %s failed", deal.lead.public_id)
            result.failed += 1


def _follow_up(campaign, result: PassResult) -> None:
    """Write again to the ones who never answered, and end the ones out of touches.

    **Ordered before openers, and that is not a priority.** Both draw from the same
    per-box headroom and the same spacing clock, so whichever runs first simply takes
    the slot; running follow-ups first means the box's last send of the day goes to a
    thread already started rather than opening one more it will never return to. The
    old arrangement — follow-ups claiming *ahead of* openers out of a separate budget —
    is what produced 102 follow-ups against 1 first email in a week, and it is
    unreachable here: there is one budget.

    **A box that is not free is skipped, not waited for.** A follow-up cannot change
    mailbox, so a busy box means its threads wait for the next pass. Others continue.

    Giving up costs no send, so it is not gated on a box at all — a deal past its last
    touch is closed whatever the pool's capacity.
    """
    from cold_outreach.core.sending_window import within_sending_window
    from cold_outreach.emails.steps.follow_up import give_up, send_follow_up
    from cold_outreach.leads.pools import awaiting_follow_up, exhausted_touches

    for deal in exhausted_touches(campaign):
        _apply(deal, give_up(deal))
        result.gave_up += 1

    if not within_sending_window():
        return

    for deal in awaiting_follow_up(campaign):
        box = deal.mailbox
        if box is None or not box.free_now() or box.headroom_today() <= 0:
            continue
        try:
            _apply(deal, send_follow_up(deal))
            result.followed_up += 1
        except Exception:
            logger.exception("follow-up to %s failed", deal.lead.public_id)
            result.failed += 1
            return


def _open_conversations(campaign, result: PassResult, prompt_line_name: str | None = None,
                        goal: int | None = None) -> None:
    """Send first emails while a box is free and somebody is waiting.

    With no ``goal`` this is exactly what it always was: the loop's bound is the
    guards themselves, `free_for_first_email` answers `None` outside the window, for
    a box at its ceiling, and for one whose spacing clock has not elapsed — and a
    send rewrites that clock, so a box takes itself out of the pool on its way past.
    The pass ends on its own, usually after one send per box.

    **With a `goal`, a closed pool is not always the end.** If every box that could
    still send is only waiting on its own spacing clock — the window is open and
    somebody has headroom — that is a wait of minutes, and `_wait_for_spacing` sleeps
    through it and the loop tries again. If instead nothing is waiting *only* on
    spacing (the window is shut, or the day's headroom is spent), `_wait_for_spacing`
    returns `None` and the loop stops there — that wall does not get slept through in
    one process, whatever `goal` asked for.

    A failed send **stops the openers for this pass** rather than moving to the next
    lead. The spacing clock is written after a successful send, so a box that just
    refused one is still "free" — retrying inside the same pass would hammer a box
    that has already said no, and would not terminate.

    **The draw happens per send, not per pass.** Drawing once outside the loop would
    make a pass a block of one move, and the log would then carry the pass's shape
    rather than the move's — the comparison would be between days, not between lines.
    A name given on the command line pins them all deliberately, which is the one case
    where a block is what the operator asked for.
    """
    from cold_outreach.core.prompt_lines import choose
    from cold_outreach.emails.models import Mailbox
    from cold_outreach.emails.steps.send import send_first_email
    from cold_outreach.leads.pools import emailable_deals

    remaining = goal
    waiting = emailable_deals(campaign).iterator()
    while remaining is None or remaining > 0:
        mailbox = Mailbox.objects.free_for_first_email()
        if mailbox is None:
            wait = _wait_for_spacing() if goal is not None else None
            if wait is None:
                return
            time.sleep(wait)
            continue
        deal = next(waiting, None)
        if deal is None:
            return
        try:
            _apply(deal, send_first_email(deal, mailbox, choose(prompt_line_name)))
            result.opened += 1
        except Exception:
            logger.exception("first email to %s failed", deal.lead.public_id)
            result.failed += 1
            return
        if remaining is not None:
            remaining -= 1


def _wait_for_spacing() -> float | None:
    """Seconds until a box next frees up purely on its spacing clock — or `None` when
    nothing is waiting *only* on that.

    `None` covers three cases that all mean the same thing to a caller deciding
    whether to sleep: the window is shut, every box with headroom left is paused for
    the rest of today, or there is no mailbox at all. Any of those is a wall measured
    in hours or a calendar day, not the ~3.5–4.5 minute spacing gap this exists to
    wait out, so a caller sees the same answer either way — stop, do not sleep.
    """
    from django.utils import timezone

    from cold_outreach.core.sending_window import within_sending_window
    from cold_outreach.emails.models import Mailbox

    if not within_sending_window():
        return None
    now = timezone.now()
    candidates = [
        box.next_send_at for box in Mailbox.objects.all()
        if box.headroom_today() > 0 and not box.free_now(now) and box.next_send_at
    ]
    if not candidates:
        return None
    # +1s: `free_now` compares with `<=`, so sleeping to the exact instant is enough
    # in theory — the second is slack for wall-clock rounding between here and there.
    return max(0.0, (min(candidates) - now).total_seconds()) + 1


def _apply(deal, next_state) -> None:
    """Persist whatever the step did to *deal*, including its new state.

    One save for the transition and the fields that justify it, so a deal can never be
    recorded as sent without its thread and timestamp, or moved out of `READY` without
    the send being recorded. A step returning `None` decided to stay put — a lead
    suppressed while the agent was writing — and that is saved just the same.
    """
    if next_state is not None:
        deal.state = next_state
    deal.save()


def _what_is_holding(campaign) -> str:
    """One line of counts, and the gate holding them, said as its consequence.

    *"No mailbox connected, so nothing can be sent"* tells the operator why a pass did
    nothing. A boolean, or a silent zero, tells them nothing at all — and a pass that
    does nothing looks exactly like a pass with nothing to do.
    """
    from cold_outreach.core.sending_window import within_sending_window
    from cold_outreach.emails.models import Mailbox
    from cold_outreach.leads.pools import emailable_deals, unanswered_replies

    waiting = emailable_deals(campaign).count()
    to_answer = unanswered_replies(campaign).count()
    counts = f"{waiting} waiting to be emailed · {to_answer} reply(ies) to answer"

    if not Mailbox.objects.exists():
        return f"{counts} · no mailbox connected, so nothing can be sent — run `outsend init`"
    headroom = Mailbox.objects.remaining_today()
    if not headroom:
        return f"{counts} · no send headroom left today, so no first emails"
    if not within_sending_window():
        return f"{counts} · outside sending hours, so no first emails"
    return f"{counts} · {headroom} first email(s) left today"
