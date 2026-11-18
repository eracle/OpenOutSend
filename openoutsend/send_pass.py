"""One pass: read the mail, move every deal that is due a move, send what the guards allow.

**A pass does what can be done *right now* and exits** — no per-recipient timers, no
loop of its own. It is the unit of work, not the unit an operator asks for: `outsend
send 5` is a *goal*, and `send_job.py` reaches it by running this repeatedly and waiting
out the clock in between. Nothing here waits, so the pass stays the thing a timer can
also fire on its own.

Every move is a step of a deal's machine (`leads/lifecycle.py`); the pass only says
which steps to try, in an order where each feeds the next:

    read      IMAP → rows → kinds → events, suppression   (`emails/mail_pass.py`)
    measure   each box's warm capacity for today, once     (`emails/warmth.py`)
    end       deals stopped or suppressed since last pass  (no send)
    wait      deals whose wait ran out become due; deals   (no send)
              that arrived too late are closed
    converse  answer every conversation with a message     (no cap, no spacing)
              waiting; close the ones gone quiet
    send      the steps that are due — one queue,          (capped, spaced, in-window)
              longest due first

Reading first is what makes the rest honest: an opt-out that arrived overnight
suppresses the person *before* anything is written to them, and a reply is visible in
the pass that answers it.

**Measuring comes second for the same reason.** A bounce is the strongest downward
signal the warm ramp has, and it arrives hours later as ordinary mail rather than as a
failed send — so it is the read that puts it on the record. Measuring before reading
would ramp a box on yesterday's evidence while today's sat unopened in it.

**Every step is cold volume**, so they share the cap. An answer inside a thread
somebody wrote in is not cold volume, and answering within minutes is more human, not
less — so a conversation's turn ignores the daily ceiling, the spacing clock and the
sending window alike.

**The waiting lives one level up.** A goal-bounded run sleeps on the spacing clock, the
daily ceiling *and* the sending window — see `send_job.py`, which asks the planner when
it could next open a conversation.

**Which box carries a due send is the planner's answer** (`planner.py`); this module
only fires steps and counts.
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
    ended: int = 0
    opened: int = 0
    stepped: int = 0
    gave_up: int = 0
    missed: int = 0
    undeliverable: int = 0
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
    """Read, move, send — once — and report. Never raises for a failed send.

    The pass moves and sends every sequence in the install, cold among them, in one
    queue. ``sequence`` (a ``core/sequences.key``) narrows that to one. Reading the mail
    and holding the conversations are never narrowed: they are the install's, whichever
    sequence the thread began in.

    ``prompt_line_name`` pins every prompt step in this pass to one move
    (``core/prompt_lines.py``); left unset, each draws its own at random, which is what
    makes the log a comparison rather than a run of whatever was pinned.

    ``narrate`` prints the holding line. A single `outsend send` wants it — it is the
    whole answer to *why did that do nothing*. A `send N` run does not want it once per
    pass, because the answer does not change while it waits; it reads ``result.holding``
    and says it when it moves.

    ``agent_draft`` opts the conversations and the prompt steps out of `AI_MODEL` — see
    ``_answer_drafted_reply`` and ``_send_agent_draft``. Unlike a failed send, this
    **does** raise (``DraftPending``), because there is nobody to report a holding line
    to: the process is about to exit.
    """
    from openoutsend.emails.mail_pass import run_mail_pass
    from openoutsend.emails.warmth import measure_pool

    result = PassResult()
    result.mirrored, result.classified, result.projected = run_mail_pass()
    measure_pool()
    _end_stopped(result, sequence)
    _run_out_waits(result, sequence)
    reached = _converse(result, agent_draft)
    _send_due(result, sequence, reached, prompt_line_name, agent_draft)
    result.holding = _what_is_holding(sequence)
    if narrate:
        logger.info("%s", result.holding)
    return result


# ── Ending and waiting ────────────────────────────────────────────


def _end_stopped(result: PassResult, sequence: str | None) -> None:
    """End every open deal whose line was piped with `stop` or whose address is suppressed.

    A suppression ends its address's deals when it lands, and ingest ends a stopped line
    as it arrives, so this finds only what neither could: a deal whose folder that run
    did not have.
    """
    from django.db.models import Q

    from openoutsend.leads.lifecycle import END_SUPPRESSED
    from openoutsend.leads.models import Deal, Suppression
    from openoutsend.planner import machines

    machine_of = machines()
    stopped = _scoped(Deal.objects.open(), sequence).filter(
        Q(stop=True) | Q(lead__email__in=Suppression.objects.values("email"))
    ).select_related("lead")
    for deal in stopped:
        _try(result, "ending", deal, machine_of(deal), END_SUPPRESSED)


def _run_out_waits(result: PassResult, sequence: str | None) -> None:
    """Move every deal whose wait ran out to its send, and close the ones that arrived
    with every step past its grace. Costs no send, so no box is asked."""
    from agentic_fsm import ProgressError

    from openoutsend.core.sequences import named
    from openoutsend.leads.lifecycle import CLOSE_MISSED, machine_for
    from openoutsend.leads.models import Deal
    from openoutsend.leads.states import WAITING

    open_sequences = (_scoped(Deal.objects.open(), sequence)
                      .values_list("sequence", flat=True).distinct())
    for name in sorted(open_sequences):
        try:
            moves = machine_for(named(name)).progress_all(WAITING)
        except ProgressError as error:
            moves = error.moves
            for entity, failure in error.failures.items():
                logger.error("deal %s of %s could not be moved: %s", entity, name, failure)
            result.failed += len(error.failures)
        result.missed += sum(move.step == CLOSE_MISSED for move in moves)


# ── The conversations ─────────────────────────────────────────────


def _converse(result: PassResult, agent_draft: AgentDraft) -> set:
    """Take a turn in every conversation with a message waiting, then close the quiet
    ones. Returns the lead ids whose message was taken up.

    The waiting conversations are listed before the loop rather than re-queried: one
    whose answer *fails* to send keeps its state, so asking again would hand back the
    same deal forever. A failure costs that one conversation this pass, and the next
    pass tries it again.

    The ids are every lead whose message was taken up, answered or not: whoever just
    wrote in is sent no step of another sequence in the same pass (``_send_due``).

    A calling agent takes the turn instead of `AI_MODEL` under ``--agent-draft``.
    """
    from openoutsend.leads.lifecycle import TURN
    from openoutsend.leads.states import CONVERSING
    from openoutsend.planner import conversations_waiting, machines

    if agent_draft.active:
        reached = _answer_drafted_reply(result, agent_draft)
    else:
        machine_of = machines()
        reached = set()
        for deal in conversations_waiting():
            reached.add(deal.lead_id)
            move = _try(result, "answer", deal, machine_of(deal), TURN)
            if move is not None:
                _count_turn(result, move.to_state == CONVERSING)
    _close_quiet(result)
    return reached


def _count_turn(result: PassResult, stayed: bool) -> None:
    if stayed:
        result.answered += 1
    else:
        result.ended += 1


def _close_quiet(result: PassResult) -> None:
    """Close each conversation whose closing gap of silence has run out.

    The step refuses while a message waits for an answer, so nobody owed one is closed.
    """
    from openoutsend.leads.lifecycle import RAN_QUIET
    from openoutsend.leads.models import Deal
    from openoutsend.leads.states import CONVERSING
    from openoutsend.planner import machines

    machine_of = machines()
    for deal in Deal.objects.in_state(CONVERSING).select_related("lead", "mailbox", "thread"):
        if _try(result, "closing", deal, machine_of(deal), RAN_QUIET) is not None:
            logger.info("%s went quiet — closing the conversation", deal.lead.public_id)
            result.gave_up += 1


def _answer_drafted_reply(result: PassResult, agent_draft: AgentDraft) -> set:
    """Send the answer already handed to the calling agent, or hand the oldest message over.

    The same one-draft-at-a-time contract as ``_send_agent_draft``, which it runs
    ahead of: somebody waiting on an answer comes before a step nobody asked for. A
    draft held for a step is left to that path, and the conversations wait behind it.

    The agent's answer is the turn: it is sent in the thread and logged as a move from
    the conversation to itself, decided by `agent`. An answer is exempt from every send
    guard, so it goes the pass it arrives. Returns every lead owed an answer, as reached.
    """
    from openoutsend.leads.lifecycle import TURN
    from openoutsend.leads.models import PendingDraft
    from openoutsend.leads.states import CONVERSING
    from openoutsend.planner import conversations_waiting, machines

    owed = conversations_waiting()
    reached = {deal.lead_id for deal in owed}
    pending = PendingDraft.objects.select_related("deal__lead").first()
    if pending is not None and pending.deal.state == CONVERSING \
            and pending.deal_id not in {deal.pk for deal in owed}:
        # Answered some other way since it was handed over (from the mailbox, by hand):
        # nothing waits in that conversation, and the draft would sit before every other.
        logger.info("%s is no longer owed an answer — dropping its draft",
                    pending.deal.lead.public_id)
        pending.delete()
        pending = None
    if pending is None:
        if owed:
            PendingDraft.objects.create(deal=owed[0])
            raise DraftPending(f"{owed[0].lead.public_id} wrote back",
                               payload=_reply_payload(owed[0]))
        return reached

    deal = next((deal for deal in owed if deal.pk == pending.deal_id), None)
    if deal is None:
        return reached
    if agent_draft.answered and not pending.answered:
        pending.body = agent_draft.body
        pending.save(update_fields=["body"])
    if not pending.answered:
        raise DraftPending(f"{deal.lead.public_id} is still waiting on a reply",
                           payload=_reply_payload(deal))

    answer = {"to": CONVERSING, "message": pending.body}
    if _try(result, "answer", deal, machines()(deal), TURN, answer=answer) is not None:
        result.answered += 1
        pending.delete()
    return reached


def _reply_payload(deal) -> dict:
    """What an agent needs to answer *deal*'s lead: their fields, the line they were
    piped with, and what `AI_MODEL` would have read — the whole thread, then how to
    answer it."""
    from openoutsend.leads.lifecycle import TURN, machine_of

    lead = deal.lead
    brief = machine_of(deal).brief(deal.pk, TURN, entity=deal)
    return {
        "lead_id": lead.public_id,
        "email": lead.email,
        "profile_text": lead.profile_text,
        "full_name": f"{lead.first_name} {lead.last_name}".strip(),
        "title": lead.title,
        "company": lead.company,
        "step": "reply",
        "subject": deal.email_subject or "",
        "fields": deal.fields,
        "prompt": brief.prompt,
    }


# ── The sends ─────────────────────────────────────────────────────


def _send_due(result: PassResult, sequence: str | None, reached: set,
              prompt_line_name: str | None = None, agent_draft: AgentDraft = OFF) -> None:
    """Send every due step the guards allow now.

    One queue, longest due first, in the planner's order (``planner.sends_due``). Its
    bound is the guards themselves: a send rewrites its box's spacing clock, so a box
    takes itself out of the pool on its way past. The pass therefore ends on its own,
    usually after one send per box, and **never sleeps**: whether a closed pool is worth
    waiting out is `send_job.py`'s question, asked with the whole pass in hand.

    A failed send **stops the sends for this pass** rather than moving to the next.
    The spacing clock is written after a successful send, so a box that just refused
    one is still "free" — retrying inside the same pass would hammer a box that has
    already said no, and would not terminate. **Except when the refusal ended the
    address** (`delivery_policy.address_is_dead`): that one is about the recipient, not
    the box, its deal is closed and cannot come round again, and stopping on it would
    put the same dead address at the head of every later pass.

    **The prompt line is drawn per send, not per pass.** Drawing once outside the loop
    would make a pass a block of one move, and the log would then carry the pass's shape
    rather than the move's. A name given on the command line pins them all deliberately,
    which is the one case where a block is what the operator asked for.

    ``agent_draft.active`` hands the prompt steps to ``_send_agent_draft`` instead — a
    calling agent answering in its own context rather than a second, separately keyed
    `AI_MODEL` call. Email steps go as they always do.

    ``reached`` holds the leads this pass has already written to, and gains every one
    written here: one message per address per pass (``planner.sends_due``).
    """
    from openoutsend.core.prompt_lines import choose
    from openoutsend.emails.steps.send import Plan
    from openoutsend.planner import machines, sends_due

    machine_of = machines()
    for deal, box in sends_due(sequence=sequence, agent_drafted=agent_draft.active,
                               reached=reached):
        plan = Plan(box, prompt_line=choose(prompt_line_name))
        try:
            _send(result, machine_of(deal), deal, plan)
        except Exception as exc:
            if _address_ended(exc, deal):
                logger.warning("%s to %s refused: the address is undeliverable, suppressed",
                               deal.due_step.path.name, deal.lead.public_id)
                result.undeliverable += 1
                continue
            logger.exception("%s to %s failed", deal.due_step.path.name, deal.lead.public_id)
            result.failed += 1
            return

    if agent_draft.active:
        _send_agent_draft(result, agent_draft, sequence, reached)


def _send(result: PassResult, machine, deal, plan) -> None:
    """Fire *deal*'s due send with *plan*, and count it as a thread opened or a later step."""
    from openoutsend.leads.lifecycle import send_name

    opens = deal.thread_id is None
    if _fire(machine, deal, send_name(deal.due_step.number), plan=plan) is None:
        return
    if opens:
        result.opened += 1
    else:
        result.stepped += 1


def _address_ended(exc: Exception, deal) -> bool:
    """True when *exc* is a refusal that suppressed *deal*'s address."""
    from openoutsend.emails.sender import Refused
    from openoutsend.leads.suppression import is_suppressed

    return isinstance(exc, Refused) and is_suppressed(deal.lead.email)


def _send_agent_draft(result: PassResult, agent_draft: AgentDraft, sequence: str | None,
                      reached: set) -> None:
    """Send the one prompt step already handed to the calling agent, or hand one back.

    Mirrors OpenOutFind's ``_resume_agent_qualification``: the pending deal does not go
    through selection again on a resume — the whole point of remembering it is that a
    second invocation answers *this* deal, not whichever one is oldest now.

    **An answer is stored the moment it arrives, sent only once a mailbox is free.**
    A box's spacing clock, daily ceiling and sending window can hold it up for hours —
    unlike a fresh candidate, an answer already given must not be asked for twice just
    because nothing was free to send it through yet.
    """
    from openoutsend.leads.models import PendingDraft
    from openoutsend.leads.states import CONVERSING
    from openoutsend.planner import due_deals

    pending = PendingDraft.objects.select_related("deal__lead")
    if sequence is not None:
        pending = pending.filter(deal__sequence=sequence)
    pending = pending.first()
    if pending is None:
        _hand_back_oldest_due(sequence)
        return
    if pending.deal.state == CONVERSING:
        return  # a reply's draft: `_answer_drafted_reply` holds it

    deal = next((due for due in due_deals(pending.deal.sequence) if due.pk == pending.deal_id),
                None)
    if deal is None:
        # Stopped, suppressed or closed since it was handed over: nothing is owed now,
        # and an answer to it must not go out.
        logger.info("%s is no longer owed a send — dropping its draft",
                    pending.deal.lead.public_id)
        pending.delete()
        return
    _store_answer(pending, agent_draft, deal)
    if not pending.answered:
        # A bare re-run with nothing answered yet — re-ask rather than silently
        # doing nothing, so the caller always gets a `draft_pending` to act on.
        raise DraftPending(f"{deal.lead.public_id} is still waiting on {deal.due_step.path.name}",
                           payload=_draft_payload(deal))

    if _send_answer(result, pending, deal, reached):
        pending.delete()


def _hand_back_oldest_due(sequence: str | None) -> None:
    """Remember the longest-due step written from a prompt, and raise it to the calling
    agent. An email step is not one: it is already written, and goes with the pass."""
    from openoutsend.leads.models import PendingDraft
    from openoutsend.planner import due_deals

    deal = next((due for due in due_deals(sequence) if not due.due_step.is_email), None)
    if deal is None:
        return
    PendingDraft.objects.create(deal=deal)
    raise DraftPending(f"{deal.lead.public_id} needs {deal.due_step.path.name}",
                       payload=_draft_payload(deal))


def _store_answer(pending, agent_draft: AgentDraft, deal) -> None:
    """Keep the answer given on this run, unless one is already kept.

    A deal's first email opens a thread and needs a subject; a later step goes under
    the thread's.
    """
    from openoutsend.errors import OutsendError

    if not agent_draft.answered or pending.answered:
        return
    if deal.thread_id is None and not agent_draft.subject:
        raise OutsendError(f"{deal.due_step.path.name} opens the thread — answer it with "
                           "--subject as well as --body")
    pending.subject = agent_draft.subject or ""
    pending.body = agent_draft.body
    pending.save(update_fields=["subject", "body"])


def _send_answer(result: PassResult, pending, deal, reached: set) -> bool:
    """Send the kept answer if its box is free. True once it is sent; a failed send keeps
    it for the next pass, as a failed `AI_MODEL` send is retried."""
    from openoutsend.emails.steps.send import Plan
    from openoutsend.planner import free_box, machines

    box = free_box(deal)
    if box is None or deal.lead_id in reached:
        # Answered, but no box is free right now, or this lead was already written
        # to this pass — the same "nothing to do this pass" every other step reports
        # quietly. The answer stays on `pending`.
        return False

    plan = Plan(box, subject=pending.subject, body=pending.body, prompt=_rendered(deal))
    try:
        _send(result, machines()(deal), deal, plan)
    except Exception:
        logger.exception("%s to %s failed", deal.due_step.path.name, deal.lead.public_id)
        result.failed += 1
        return False
    reached.add(deal.lead_id)
    return True


def _draft_payload(deal) -> dict:
    """What an agent needs to write *deal*'s due step: the lead's own fields, the same
    ones `AI_MODEL` sees, the step's file and its rendered prompt, and, for a later
    step, the thread's subject it goes under."""
    lead = deal.lead
    return {
        "lead_id": lead.public_id,
        "profile_text": lead.profile_text,
        "full_name": f"{lead.first_name} {lead.last_name}".strip(),
        "title": lead.title,
        "company": lead.company,
        "step": deal.due_step.path.name,
        "subject": deal.email_subject or "",
        "prompt": _rendered(deal),
    }


def _rendered(deal) -> str:
    """*deal*'s due step, rendered for it with no prompt line drawn."""
    from openoutsend.emails.steps.send import step_fields

    return deal.due_step.render(**step_fields(deal, None))


# ── Firing ────────────────────────────────────────────────────────


def _scoped(deals, sequence: str | None):
    """*deals* in *sequence* alone, or all of them when no sequence is named."""
    return deals if sequence is None else deals.filter(sequence=sequence)


def _try(result: PassResult, doing: str, deal, machine, step: str, **options):
    """``_fire``, with a failure logged and counted instead of raised. Returns the move."""
    try:
        return _fire(machine, deal, step, **options)
    except Exception:
        logger.exception("%s %s failed", doing, deal.lead.public_id)
        result.failed += 1
        return None


def _fire(machine, deal, step: str, *, plan=None, answer=None):
    """Fire *machine*'s *step* on *deal*, in the machine's one transaction. Returns the move.

    The machine locks the deal's state, runs the step and writes the move in one
    transaction, so the mail-log row, the transport call, the deal's fields and its new
    state commit together, after the send: a pass killed before the commit leaves nothing
    half-recorded — no row, the deal where it was, and the step due again on the next
    pass. A second process firing the same deal waits for this one. ``None`` means the
    step said "not now". A refusal rolls the transaction back too, so its row and
    failure event are written again outside it, because the bounce rate counts refusals.
    """
    from openoutsend.emails.sender import Refused

    deal.plan = plan
    try:
        return machine.fire(deal.pk, step, entity=deal, answer=answer)
    except Refused as refused:
        refused.record_again()
        raise


def _what_is_holding(sequence: str | None) -> str:
    """One line of counts, and the gate holding them, said as its consequence.

    *"No mailbox connected, so nothing can be sent"* tells the operator why a pass did
    nothing. A boolean, or a silent zero, tells them nothing at all — and a pass that
    does nothing looks exactly like a pass with nothing to do.
    """
    from openoutsend.core.sending_window import lead_timezone, within_sending_window
    from openoutsend.emails.models import Mailbox
    from openoutsend.planner import conversations_waiting, waiting_to_open

    pool = waiting_to_open(sequence)
    counts = (f"{pool.count()} waiting to be emailed · "
              f"{len(conversations_waiting())} reply(ies) to answer")

    if not Mailbox.objects.exists():
        return f"{counts} · no mailbox connected, so nothing can be sent — run `outsend check`"
    headroom = Mailbox.objects.remaining_today(sequence)
    if not headroom:
        return f"{counts} · no send headroom left today, so no first emails"
    zones = {lead_timezone(deal.lead) for deal in pool} or {None}
    if not any(within_sending_window(zone=zone) for zone in zones):
        return f"{counts} · outside sending hours for every waiting lead, so no first emails"
    return f"{counts} · {headroom} first email(s) left today"
