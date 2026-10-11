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
    close     deals past their last touch, or never due    (no send)
    send      openers, later steps, follow-ups — one       (capped, spaced, in-window)
              queue, oldest due first


Reading first is what makes the rest honest: an opt-out that arrived overnight
suppresses the person *before* anything is written to them, and a reply is visible in
the pass that answers it.

**Measuring comes second for the same reason.** A bounce is the strongest downward
signal the warm ramp has, and it arrives hours later as ordinary mail rather than as a
failed send — so it is the read that puts it on the record. Measuring before reading
would ramp a box on yesterday's evidence while today's sat unopened in it.

**Every step and follow-up is cold volume**, so they share the cap. A reply inside a
thread somebody started is not cold volume, and answering within minutes is more
human, not less — so replies ignore the daily ceiling, the spacing clock and the
sending window alike.

**The waiting lives one level up.** A goal-bounded run sleeps on the spacing clock, the
daily ceiling *and* the sending window — see `send_job.py`, which reaches a goal by
running this pass repeatedly and asking the planner when it could next open a conversation.

**What is due and which box carries it are the planner's answers** (`planner.py`); this
module only writes, sends and counts.
Keeping the wait out of here is what leaves the pass firable by a timer, and what lets
each wait be spent reading the mail rather than blocking inside one step of it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from openoutsend.core.agent_draft import OFF, AgentDraft
from openoutsend.errors import DraftPending

logger = logging.getLogger(__name__)


@dataclass
class PassResult:
    """What one pass did. The counts are what `outsend send` prints."""

    mirrored: int = 0
    classified: int = 0
    projected: int = 0
    answered: int = 0
    opened: int = 0
    stepped: int = 0
    followed_up: int = 0
    gave_up: int = 0
    missed: int = 0
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


def run_send_pass(prompt_line_name: str | None = None, narrate: bool = True,
                  agent_draft: AgentDraft = OFF, sequence: str | None = None) -> PassResult:
    """Read, answer, open — once — and report. Never raises for a failed send.

    The pass closes and sends every sequence in the install, cold among them, in one
    queue. ``sequence`` (a ``core/sequences.key``) narrows that to one. Reading the mail
    and answering replies are never narrowed: they are the install's, whichever
    sequence the thread began in.

    ``prompt_line_name`` pins every opener in this pass to one move
    (``core/prompt_lines.py``); left unset, each opener draws its own at random, which
    is what makes the log a comparison rather than a run of whatever was pinned.

    ``narrate`` prints the holding line. A single `outsend send` wants it — it is the
    whole answer to *why did that do nothing*. A `send N` run does not want it once per
    pass, because the answer does not change while it waits; it reads ``result.holding``
    and says it when it moves.

    ``agent_draft`` opts the openers out of `AI_MODEL` — see ``_send_cold``.
    Unlike a failed send, this **does** raise (``DraftPending``), because there is
    nobody to report a holding line to: the process is about to exit.
    """
    from openoutsend.emails.mail_pass import run_mail_pass
    from openoutsend.emails.warmth import measure_pool

    result = PassResult()
    result.mirrored, result.classified, result.projected = run_mail_pass()
    measure_pool()
    _answer_replies(result)
    _close_ended(result, sequence)
    _send_cold(result, sequence, prompt_line_name, agent_draft)
    result.holding = _what_is_holding(sequence)
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
    from openoutsend.emails.steps.reply import answer_reply
    from openoutsend.planner import unanswered_replies

    for deal in list(unanswered_replies()):
        try:
            _apply(deal, answer_reply(deal))
            result.answered += 1
        except Exception:
            logger.exception("reply to %s failed", deal.lead.public_id)
            result.failed += 1


def _close_ended(result: PassResult, sequence: str | None) -> None:
    """End the deals past their last touch, and the ones that can never become due.

    Closing costs no send, so it is not gated on a box at all — a deal is closed
    whatever the pool's capacity.
    """
    from openoutsend.emails.steps.follow_up import give_up
    from openoutsend.leads.models import DealState, Outcome
    from openoutsend.planner import exhausted_touches, missed_enrolments

    for deal in exhausted_touches(sequence):
        _apply(deal, give_up(deal))
        result.gave_up += 1

    for deal in missed_enrolments(sequence):
        logger.info("%s arrived with every step past its grace — closing", deal.lead.public_id)
        deal.outcome = Outcome.MISSED
        _apply(deal, DealState.COMPLETED)
        result.missed += 1


def _send_cold(result: PassResult, sequence: str | None, prompt_line_name: str | None = None,
               agent_draft: AgentDraft = OFF) -> None:
    """Send every cold email the guards allow now — openers, later steps, follow-ups.

    One queue, oldest due first, in the planner's order (``planner.cold_sends_due``).
    Its bound is the guards themselves: a send rewrites its box's spacing clock, so a
    box takes itself out of the pool on its way past. The pass therefore ends on its
    own, usually after one send per box, and **never sleeps**: whether a closed pool is
    worth waiting out is `send_job.py`'s question, asked with the whole pass in hand.

    A failed send **stops the cold sends for this pass** rather than moving to the next.
    The spacing clock is written after a successful send, so a box that just refused
    one is still "free" — retrying inside the same pass would hammer a box that has
    already said no, and would not terminate.

    **The draw happens per send, not per pass.** Drawing once outside the loop would
    make a pass a block of one move, and the log would then carry the pass's shape
    rather than the move's — the comparison would be between days, not between lines.
    A name given on the command line pins them all deliberately, which is the one case
    where a block is what the operator asked for.

    ``agent_draft.active`` hands the openers to ``_open_with_agent_draft`` instead — a
    calling agent answering in its own context rather than a second, separately keyed
    `AI_MODEL` call. Steps and follow-ups go as they always do.
    """
    from openoutsend.planner import cold_sends_due

    for send, box in cold_sends_due(sequence=sequence, openers=not agent_draft.active):
        try:
            _apply(send.deal, _write_and_send(send, box, prompt_line_name))
        except Exception:
            logger.exception("%s to %s failed", send.kind, send.deal.lead.public_id)
            result.failed += 1
            return
        _count(result, send.kind)

    if agent_draft.active:
        _open_with_agent_draft(result, agent_draft, sequence)


def _write_and_send(send, box, prompt_line_name: str | None):
    """Hand one due send to the step that writes its kind. Returns the deal's next state."""
    from openoutsend.core.prompt_lines import choose
    from openoutsend.emails.steps.follow_up import send_follow_up
    from openoutsend.emails.steps.send import send_first_email, send_next_step
    from openoutsend.planner import OPENER, STEP

    if send.kind == OPENER:
        return send_first_email(send.deal, box, send.step, choose(prompt_line_name))
    if send.kind == STEP:
        return send_next_step(send.deal, send.step, choose(prompt_line_name))
    return send_follow_up(send.deal)


def _count(result: PassResult, kind: str) -> None:
    """Add one send of *kind* to the pass's counts."""
    from openoutsend.planner import OPENER, STEP

    if kind == OPENER:
        result.opened += 1
    elif kind == STEP:
        result.stepped += 1
    else:
        result.followed_up += 1


def _open_with_agent_draft(result: PassResult, agent_draft: AgentDraft, sequence: str | None) -> None:
    """Answer the one deal already handed to the calling agent, or hand one back.

    Mirrors OpenOutFind's ``_resume_agent_qualification``: the pending deal does not
    go through ``openers_due()`` selection again on a resume — the whole point of
    remembering it is that a second invocation answers *this* deal, not whichever one
    happens to be oldest now.

    **An answer is stored the moment it arrives, sent only once a mailbox is free.**
    A box's spacing clock, daily ceiling and sending window can hold it up for hours —
    unlike a fresh candidate, an answer already given must not be asked for twice just
    because nothing was free to send it through yet.
    """
    from openoutsend.core.sequences import for_deal
    from openoutsend.emails.steps.send import send_drafted_email
    from openoutsend.leads.models import PendingDraft
    from openoutsend.planner import free_for_first_email, openers_due, opening_step

    pending = PendingDraft.objects.select_related("deal__lead")
    if sequence is not None:
        pending = pending.filter(deal__sequence=sequence)
    pending = pending.first()
    if pending is None:
        due = openers_due(sequence=sequence)
        if not due:
            return
        deal = due[0].deal
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

    mailbox = free_for_first_email()
    if mailbox is None:
        # Answered, but no box is free right now — the same "nothing to do this
        # pass" every other step reports quietly. The answer stays on `pending`.
        return

    deal = pending.deal
    # Asked again rather than remembered: the answer was given for this deal's opening
    # step, and that is the one it is sent as.
    step, _ = opening_step(deal, for_deal(deal))
    try:
        _apply(deal, send_drafted_email(deal, mailbox, step, pending.subject, pending.body, None))
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


def _what_is_holding(sequence: str | None) -> str:
    """One line of counts, and the gate holding them, said as its consequence.

    *"No mailbox connected, so nothing can be sent"* tells the operator why a pass did
    nothing. A boolean, or a silent zero, tells them nothing at all — and a pass that
    does nothing looks exactly like a pass with nothing to do.
    """
    from openoutsend.core.sending_window import within_sending_window
    from openoutsend.emails.models import Mailbox
    from openoutsend.planner import emailable_deals, unanswered_replies

    waiting = emailable_deals(sequence).count()
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
