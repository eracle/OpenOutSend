"""One pass: read the mail, answer what came back, open what there is room to open.

**A pass does what the guards allow *right now* and exits** — no per-recipient timers,
no loop of its own. It is the unit of work, not the unit an operator asks for: `outsend
send 5` is a *goal*, and `send_job.py` reaches it by running this repeatedly and waiting
out the clock in between. Nothing here waits, so the pass stays the thing a timer can
also fire on its own.

The order is the design, and each step feeds the next:

    read      IMAP → rows → kinds → events, suppression   (`emails/mail_pass.py`)
    measure   each box's warm capacity for today, once     (`emails/warmth.py`)
    answer    every thread the lead has replied in         (no cap, no spacing)
    open      one first email per box that is free now     (capped, spaced, in-window)

Reading first is what makes the rest honest: an opt-out that arrived overnight
suppresses the person *before* anything is written to them, and a reply is visible in
the pass that answers it.

**Measuring comes second for the same reason.** A bounce is the strongest downward
signal the warm ramp has, and it arrives hours later as ordinary mail rather than as a
failed send — so it is the read that puts it on the record. Measuring before reading
would ramp a box on yesterday's evidence while today's sat unopened in it.

**Openers are the only cold volume**, so they are the only thing under a cap. A reply
inside a thread somebody started is not cold volume, and answering within minutes is
more human, not less — so replies ignore the daily ceiling, the spacing clock and the
sending window alike.

**The waiting lives one level up.** A goal-bounded run sleeps on the spacing clock, the
daily ceiling *and* the sending window — see `send_job.py`, which reaches a goal by
running this pass repeatedly and asking the pool when it could next open a conversation.
Keeping the wait out of here is what leaves the pass firable by a timer, and what lets
each wait be spent reading the mail rather than blocking inside one step of it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from cold_outreach.core.agent_draft import OFF, AgentDraft
from cold_outreach.errors import DraftPending

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

    holding: str = ""
    """The counts line and the gate holding them, as ``_what_is_holding`` phrased it.

    Carried on the result rather than only logged, because a run of many passes must be
    able to print it *when it changes* instead of once a slice all night.
    """

    @property
    def ok(self) -> bool:
        """True when nothing raised on its way out."""
        return self.failed == 0


def run_send_pass(prompt_line_name: str | None = None,
                  narrate: bool = True, agent_draft: AgentDraft = OFF) -> PassResult:
    """Read, answer, open — once — and report. Never raises for a failed send.

    ``prompt_line_name`` pins every opener in this pass to one move
    (``core/prompt_lines.py``); left unset, each opener draws its own at random, which
    is what makes the log a comparison rather than a run of whatever was pinned.

    ``narrate`` prints the holding line. A single `outsend send` wants it — it is the
    whole answer to *why did that do nothing*. A `send N` run does not want it once per
    pass, because the answer does not change while it waits; it reads ``result.holding``
    and says it when it moves.

    ``agent_draft`` opts the opener step out of `AI_MODEL` — see ``_open_conversations``.
    Unlike a failed send, this **does** raise (``DraftPending``), because there is
    nobody to report a holding line to: the process is about to exit.
    """
    from cold_outreach.emails.mail_pass import run_mail_pass
    from cold_outreach.emails.warmth import measure_pool

    result = PassResult()
    result.mirrored, result.classified, result.projected = run_mail_pass()
    measure_pool()
    _answer_replies(result)
    _follow_up(result)
    _open_conversations(result, prompt_line_name, agent_draft)
    result.holding = _what_is_holding()
    if narrate:
        logger.info("%s", result.holding)
    return result


def _answer_replies(result: PassResult) -> None:
    """Answer every thread whose newest turn is theirs.

    The pool is materialised before the loop rather than re-queried: a deal whose
    reply *fails* to send keeps its state, so asking the database again would hand
    back the same row forever. A failure costs that one conversation this pass, and
    the next pass tries it again.
    """
    from cold_outreach.emails.steps.reply import answer_reply
    from cold_outreach.leads.pools import unanswered_replies

    for deal in list(unanswered_replies()):
        try:
            _apply(deal, answer_reply(deal))
            result.answered += 1
        except Exception:
            logger.exception("reply to %s failed", deal.lead.public_id)
            result.failed += 1


def _follow_up(result: PassResult) -> None:
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

    for deal in exhausted_touches():
        _apply(deal, give_up(deal))
        result.gave_up += 1

    if not within_sending_window():
        return

    for deal in awaiting_follow_up():
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


def _open_conversations(result: PassResult, prompt_line_name: str | None = None,
                        agent_draft: AgentDraft = OFF) -> None:
    """Send first emails while a box is free and somebody is waiting.

    The loop's bound is the guards themselves: `free_for_first_email` answers `None`
    outside the window, for a box at its ceiling, and for one whose spacing clock has
    not elapsed — and a send rewrites that clock, so a box takes itself out of the
    pool on its way past. The pass therefore ends on its own, usually after one send
    per box, and **never sleeps**: a closed pool ends the pass, and whether that is
    worth waiting out is `send_job.py`'s question, asked with the whole pass in hand.

    A failed send **stops the openers for this pass** rather than moving to the next
    lead. The spacing clock is written after a successful send, so a box that just
    refused one is still "free" — retrying inside the same pass would hammer a box
    that has already said no, and would not terminate.

    **The draw happens per send, not per pass.** Drawing once outside the loop would
    make a pass a block of one move, and the log would then carry the pass's shape
    rather than the move's — the comparison would be between days, not between lines.
    A name given on the command line pins them all deliberately, which is the one case
    where a block is what the operator asked for.

    ``agent_draft.active`` hands this whole step to ``_open_with_agent_draft`` instead
    — a calling agent answering in its own context rather than a second, separately
    keyed `AI_MODEL` call.
    """
    from cold_outreach.core.prompt_lines import choose
    from cold_outreach.emails.models import Mailbox
    from cold_outreach.emails.steps.send import send_first_email
    from cold_outreach.leads.pools import emailable_deals

    if agent_draft.active:
        _open_with_agent_draft(result, agent_draft)
        return

    waiting = emailable_deals().iterator()
    while (mailbox := Mailbox.objects.free_for_first_email()) is not None:
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


def _open_with_agent_draft(result: PassResult, agent_draft: AgentDraft) -> None:
    """Answer the one deal already handed to the calling agent, or hand one back.

    Mirrors OpenOutFind's ``_resume_agent_qualification``: the pending deal does not
    go through ``emailable_deals()`` selection again on a resume — the whole point of
    remembering it is that a second invocation answers *this* deal, not whichever one
    happens to be oldest now.

    **An answer is stored the moment it arrives, sent only once a mailbox is free.**
    A box's spacing clock, daily ceiling and sending window can hold it up for hours —
    unlike a fresh candidate, an answer already given must not be asked for twice just
    because nothing was free to send it through yet.
    """
    from cold_outreach.emails.models import Mailbox
    from cold_outreach.emails.steps.send import send_drafted_email
    from cold_outreach.leads.models import PendingDraft
    from cold_outreach.leads.pools import emailable_deals

    pending = PendingDraft.objects.select_related("deal__lead").first()
    if pending is None:
        deal = next(emailable_deals().iterator(), None)
        if deal is None:
            return
        PendingDraft.objects.create(deal=deal)
        raise DraftPending(f"{deal.lead.public_id} needs an opener",
                            payload=_candidate_payload(deal.lead))

    if agent_draft.answered and not pending.answered:
        pending.subject = agent_draft.subject
        pending.body = agent_draft.body
        pending.save(update_fields=["subject", "body"])

    if not pending.answered:
        # A bare re-run with nothing answered yet — re-ask rather than silently
        # doing nothing, so the caller always gets a `draft_pending` to act on.
        raise DraftPending(f"{pending.deal.lead.public_id} is still waiting on an opener",
                            payload=_candidate_payload(pending.deal.lead))

    mailbox = Mailbox.objects.free_for_first_email()
    if mailbox is None:
        # Answered, but no box is free right now — the same "nothing to do this
        # pass" every other step reports quietly. The answer stays on `pending`.
        return

    deal = pending.deal
    try:
        _apply(deal, send_drafted_email(deal, mailbox, pending.subject, pending.body, None))
        result.opened += 1
    except Exception:
        logger.exception("first email to %s failed", deal.lead.public_id)
        result.failed += 1
        return
    pending.delete()


def _candidate_payload(lead) -> dict:
    """The fields an agent needs to write the opener — the same ones `AI_MODEL` sees."""
    return {
        "lead_id": lead.public_id,
        "profile_text": lead.profile_text,
        "full_name": f"{lead.first_name} {lead.last_name}".strip(),
        "title": lead.title,
        "company": lead.company,
    }


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


def _what_is_holding() -> str:
    """One line of counts, and the gate holding them, said as its consequence.

    *"No mailbox connected, so nothing can be sent"* tells the operator why a pass did
    nothing. A boolean, or a silent zero, tells them nothing at all — and a pass that
    does nothing looks exactly like a pass with nothing to do.
    """
    from cold_outreach.core.sending_window import within_sending_window
    from cold_outreach.emails.models import Mailbox
    from cold_outreach.leads.pools import emailable_deals, unanswered_replies

    waiting = emailable_deals().count()
    to_answer = unanswered_replies().count()
    counts = f"{waiting} waiting to be emailed · {to_answer} reply(ies) to answer"

    if not Mailbox.objects.exists():
        return f"{counts} · no mailbox connected, so nothing can be sent — run `outsend check`"
    headroom = Mailbox.objects.remaining_today()
    if not headroom:
        return f"{counts} · no send headroom left today, so no first emails"
    if not within_sending_window():
        return f"{counts} · outside sending hours, so no first emails"
    return f"{counts} · {headroom} first email(s) left today"
