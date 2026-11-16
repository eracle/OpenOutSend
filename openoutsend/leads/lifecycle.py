"""The deal machine: every way a deal's state may change, in one list.

    agfsm --machine openoutsend.leads.lifecycle:deals show

A deal's state is kept by `agentic_fsm` (its Django backend's two tables, in this store):
one current-state row per deal and an append-only log of every move, with the step that
made it. Nothing writes a deal's state except a `fire` of one of the steps below, so the
list at the bottom of this file is the whole graph.

**What a step sends is chosen before it runs.** The send guards (warm capacity, pacing,
the sending window) pick the box, the step and the prompt line, and that is timing, which
is the caller's: the send pass (`send_pass.py`) asks the planner, sets the answer on the
deal as its `plan`, and fires with the deal in hand (`fire(id, step, entity=deal)`). A
`fire` with no deal in hand (the `agfsm` CLI, `progress`, `progress_all`) goes through `load_deal`, which
asks the planner the same question for that one deal, so a move made from the CLI is one
the pass could have made right now, and no other. A step whose plan is not its own kind
says "not now".

**A step saves the deal itself**, the fields its send wrote and the outcome, whether it
moves the deal or not, and the fire runs in one transaction that holds the deal's state
row from read to move, so the send's row, the deal and the move commit together.

**Sending into a live thread is not a move.** A reply, a later step and a follow-up leave
the deal `EMAILED` and return `None`; the mail log is the record that they went.
"""
from __future__ import annotations

from dataclasses import dataclass

from agentic_fsm import Machine

from openoutsend.leads.states import MACHINE, DealState
from openoutsend.planner import FOLLOW_UP, OPENER, STEP

READY, EMAILED, COMPLETED = DealState.READY, DealState.EMAILED, DealState.COMPLETED

# The kinds of plan beside the planner's three cold sends (`planner.OPENER`, `STEP`,
# `FOLLOW_UP`), which keep their own names.
REPLY = "reply"
GIVE_UP = "give_up"
MISSED = "missed"


@dataclass(frozen=True)
class Plan:
    """What the deal is owed now, as the planner chose it.

    ``body`` set means a calling agent wrote the message (``send --agent-draft``), with
    the ``subject`` an opener needs and the rendered ``prompt`` a later step records.
    """

    kind: str
    step: object = None
    box: object = None
    prompt_line: object = None
    subject: str = ""
    body: str = ""
    prompt: str = ""


# ── Loading ──


def load_deal(deal_id: str):
    """The deal, with the planner's ``plan`` for it now attached (``None``: owed nothing)."""
    from openoutsend.leads.models import Deal

    deal = Deal.objects.select_related("lead", "mailbox").get(pk=deal_id)
    deal.plan = plan_for(deal)
    return deal


def plan_for(deal) -> Plan | None:
    """What the send pass would do to *deal* if it ran now, asked of the planner's pools.

    In the pass's own order: a reply first, then closing, then the cold sends, which only
    come with a box the guards say is free. A reply held for an agent
    (``OUTSEND_REPLIES=hold``) is owed nothing here.
    """
    from openoutsend.core.conf import REPLIES_HOLD, replies
    from openoutsend.core.prompt_lines import choose
    from openoutsend.planner import cold_sends_due, exhausted_touches, missed_enrolments, unanswered_replies

    if unanswered_replies().filter(pk=deal.pk).exists():
        return Plan(REPLY) if replies() != REPLIES_HOLD else None
    if any(each.pk == deal.pk for each in exhausted_touches(deal.sequence)):
        return Plan(GIVE_UP)
    if any(each.pk == deal.pk for each in missed_enrolments(deal.sequence)):
        return Plan(MISSED)
    for send, box in cold_sends_due(sequence=deal.sequence):
        if send.deal.pk == deal.pk:
            return Plan(send.kind, step=send.step, box=box, prompt_line=choose(None))
    return None


# ── Steps ──


def end_suppressed(deal):
    """End an open deal whose address is on the suppression list. Sends nothing.

    The caller that suppressed the address sets the outcome first (`unsubscribed` or
    `undeliverable`); one fired with none is taken for an opt-out.
    """
    from openoutsend.leads.models import Outcome
    from openoutsend.leads.suppression import is_suppressed

    if not is_suppressed(deal.lead.email):
        return None
    deal.outcome = deal.outcome or Outcome.UNSUBSCRIBED
    return _saved(deal, COMPLETED)


def send_opener(deal):
    """Open the conversation from the planned box with the planned step."""
    from openoutsend.emails.steps import send

    plan = _plan(deal, OPENER)
    if plan is None:
        return None
    if plan.body:
        state = send.send_drafted_email(deal, plan.box, plan.step, plan.subject, plan.body, None)
    else:
        state = send.send_first_email(deal, plan.box, plan.step, plan.prompt_line)
    return _saved(deal, state)


def close_missed(deal):
    """End a deal every step of whose sequence was past its grace when it arrived."""
    from openoutsend.leads.models import Outcome

    if _plan(deal, MISSED) is None:
        return None
    deal.outcome = Outcome.MISSED
    return _saved(deal, COMPLETED)


def answer_reply(deal):
    """Answer the lead who wrote back: the model decides, or the calling agent's draft goes."""
    from openoutsend.emails.steps import reply

    plan = _plan(deal, REPLY)
    if plan is None:
        return None
    state = reply.send_drafted_reply(deal, plan.body) if plan.body else reply.answer_reply(deal)
    return _saved(deal, state)


def send_step(deal):
    """Send the sequence's next step in the deal's thread, from its own box."""
    from openoutsend.emails.steps import send

    plan = _plan(deal, STEP)
    if plan is None:
        return None
    if plan.body:
        state = send.send_drafted_step(deal, plan.step, plan.body, plan.prompt)
    else:
        state = send.send_next_step(deal, plan.step, plan.prompt_line)
    return _saved(deal, state)


def follow_up(deal):
    """Write once more in the thread of a lead who never answered."""
    from openoutsend.emails.steps import follow_up as follow_ups

    if _plan(deal, FOLLOW_UP) is None:
        return None
    return _saved(deal, follow_ups.send_follow_up(deal))


def give_up(deal):
    """End a pursuit nobody answered, once its last step and follow-ups are spent."""
    from openoutsend.emails.steps import follow_up as follow_ups

    if _plan(deal, GIVE_UP) is None:
        return None
    return _saved(deal, follow_ups.give_up(deal))


def _plan(deal, kind: str) -> Plan | None:
    plan = getattr(deal, "plan", None)
    return plan if plan is not None and plan.kind == kind else None


def _saved(deal, state):
    deal.save()
    return state


# ── The graph ──

# fmt: off
TRANSITIONS = [
    # from                to          step
    ((READY, EMAILED),    COMPLETED,  end_suppressed),
    (READY,               EMAILED,    send_opener),
    (READY,               COMPLETED,  close_missed),
    (EMAILED,             COMPLETED,  answer_reply),
    (EMAILED,             COMPLETED,  send_step),
    (EMAILED,             COMPLETED,  follow_up),
    (EMAILED,             COMPLETED,  give_up),
]
# fmt: on

deals = Machine(DealState, TRANSITIONS, load=load_deal, backend="agentic_fsm.backends.django", name=MACHINE)
