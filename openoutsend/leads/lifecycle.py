"""The deal machine: a sequence folder, read as a machine of waits, sends and one conversation.

    agfsm --machine openoutsend.leads.lifecycle:cold show

Every folder becomes the same shape, built when it is asked for (`machine_for`), one
machine per folder under the folder's name (`leads/states.machine_name`):

    awaiting_E<k>   a wait: step k's gap, after the deal's `start_at` for the step it
                    opens with, in business days after step k-1 went for the rest
    sending_E<k>    step k is due and waits for a box the send guards say is free
    conversing      after the last step, when the install holds conversations
                    (``OUTSEND_CONVERSATIONS``, `core/conf.py`): the model answers
                    whoever wrote back, and silence closes the deal
    completed       over, with `Deal.outcome` saying why

A deal's state is kept by `agentic_fsm` (its Django backend's two tables, in this
store): one current-state row per deal and an append-only log of every move. Each state
has one step, declared below, and nothing writes a deal's state except that step, so a
folder's edges are its whole graph, and a move's time in the log is what the next wait
counts from. Nothing about timing is kept on the deal.

**Every state's step first ends a deal that must stop**: one whose address is
suppressed, or whose line was piped with `stop`. Then it does its own work.

**Neither half of a step decides.** The wait runs out, the send sends: an email
template as written, a prompt template through the model, and the deal moves on either
way. A reply never stops or skips a step. A deal that wrote back is answered in
`conversing`, where the model reads the whole thread; an install that holds no
conversations ends the deal `sent` with its last step, and a reply is only stored.

**Which box sends is chosen before the send runs.** The send guards (warm capacity,
pacing, the sending window) are the caller's: the send pass (`send_pass.py`) sets what
they chose on the deal as its `plan` and fires with the deal in hand. A fire with no
deal in hand (the `agfsm` CLI, `progress`) asks the planner for that one deal, so a
send made from the CLI is one the pass could have made right now, and no other.

**A step saves the deal itself**, the fields its send wrote and the outcome, and the
fire runs in one transaction that holds the deal's state row from read to move, so the
send's row, the deal and the move commit together.
"""
from __future__ import annotations

import types
from functools import lru_cache

from agentic_fsm import FinalState, InitialState, State, States, Step

from openoutsend.leads.states import (
    COMPLETED,
    CONVERSING,
    SENDING,
    WAITING,
    awaiting,
    machine_name,
    sending,
)

# ── Finding a deal's machine ──


def machine_of(deal):
    """The machine *deal* moves through: its sequence folder's."""
    from openoutsend.core.sequences import for_deal

    return machine_for(for_deal(deal))


def machine_for(sequence):
    """The machine a sequence folder is, built once per folder as it reads now."""
    from openoutsend.core.conf import conversations

    return _machine(sequence, conversations())


@lru_cache(maxsize=64)
def _machine(sequence, converses: bool):
    from openoutsend.machines import MailMachine

    states = _states(sequence, converses)
    return MailMachine(
        states, _edges(sequence, states, converses),
        load=load_deal,
        address=lambda deal: deal.lead.email,
        unsubscribe_url=_unsubscribe_url,
        name=machine_name(sequence.name),
    )


def __getattr__(name: str):
    """A sequence's machine as an attribute of this module, for ``agfsm --machine``.

    ``openoutsend.leads.lifecycle:cold`` is the built-in sequence's; any other name is
    a folder under the operator's sequences directory.
    """
    from openoutsend.core.sequences import SequenceError, named

    if name.startswith("__"):
        raise AttributeError(name)
    try:
        return machine_for(named(name))
    except SequenceError as error:
        raise AttributeError(str(error)) from error


def load_deal(deal_id: str):
    """The deal with its lead and box, as every step reads it."""
    from openoutsend.leads.models import Deal

    return Deal.objects.select_related("lead", "mailbox", "thread").get(pk=deal_id)


def _unsubscribe_url(deal) -> str:
    from openoutsend.emails.steps.send import unsubscribe_url

    return unsubscribe_url(deal)


# ── When a step is due ──


def opening_step(deal, sequence) -> tuple | None:
    """The step a deal not yet written to opens with, and when it is due — or ``None``.

    ``E0`` is due its days after ``start_at``, and each later step its days after the
    one before. A step already more than ``GRACE_DAYS`` overdue when the deal was
    enrolled is skipped (``core/conf.py`` says why), and the next counts from when the
    skipped one was due, so skipping never pushes the rest of the sequence later.
    ``None`` when every step was past its grace on arrival. Days are the lead's
    (``core/business_time.py``).
    """
    from openoutsend.core.business_time import add_business_days, business_days_between
    from openoutsend.core.conf import GRACE_DAYS
    from openoutsend.core.sending_window import lead_timezone

    zone = lead_timezone(deal.lead)
    due = deal.start_at
    for step in sequence.steps:
        due = add_business_days(due, step.days, zone)
        if business_days_between(due, deal.created_at, zone) <= GRACE_DAYS:
            return step, due
    return None


def _step_due(sequence, step):
    """When a deal resting in ``awaiting_E<k>`` is due *step*, as its wait asks it.

    A deal written to counts the step's days from the move that brought it here, which
    is the send of the step before. One not yet written to is waiting for the step it
    opens with, due where ``opening_step`` puts it.
    """

    def due_at(deal, entered_at):
        from openoutsend.core.business_time import add_business_days
        from openoutsend.core.sending_window import lead_timezone

        if deal.thread_id is None:
            opening = opening_step(deal, sequence)
            if opening is not None and opening[0].number == step.number:
                return opening[1]
        return add_business_days(entered_at, step.days, lead_timezone(deal.lead))

    return due_at


def _quiet_until(sequence):
    """When a conversation last heard from at *entered_at* closes for silence."""
    from openoutsend.core.conf import CONVERSATION_CLOSES_AFTER_BUSINESS_DAYS

    own = sequence.conversation
    days = own.days if own and own.days else CONVERSATION_CLOSES_AFTER_BUSINESS_DAYS

    def closes_at(deal, entered_at):
        from openoutsend.core.business_time import add_business_days
        from openoutsend.core.sending_window import lead_timezone

        return add_business_days(entered_at, days, lead_timezone(deal.lead))

    return closes_at


# ── The steps ──


class DealStep(Step):
    """A deal state's step: end a deal that must stop, else do the state's own work.

    A subclass says what the state does (``proceed``) and why a deal it ends itself is
    over (``closes_as``). The outcome is written with the move, as the step's effect.
    """

    reads_only = True
    closes_as = ""

    def run(self, deal, targets: tuple):
        if _must_stop(deal):
            return _target(targets, COMPLETED)
        return self.proceed(deal, targets)

    def proceed(self, deal, targets: tuple):
        raise NotImplementedError

    def effect(self, deal, result) -> None:
        if result != COMPLETED:
            return
        deal.outcome = _stopped_as(deal) or self.closes_as
        deal.save()


class Awaiting(DealStep):
    """Waiting for a step's day. The first one also places a deal that arrived late:
    at the first step not past its grace, or at the end when every step was."""

    def __init__(self, sequence, step):
        from openoutsend.leads.models import Outcome
        from openoutsend.machines import Wait

        self.sequence = sequence
        self.number = step.number
        self.wait = Wait(rests=_step_due(sequence, step), to=sending(step.number))
        self.closes_as = Outcome.MISSED

    def proceed(self, deal, targets: tuple):
        if self.number == 0:
            opening = opening_step(deal, self.sequence)
            if opening is None:
                return _target(targets, COMPLETED)
            if opening[0].number != 0:
                return _target(targets, awaiting(opening[0].number))
        return self.wait


class Sending(DealStep):
    """Sending a step that is due: as written, or through the model when it is a prompt.

    ``file`` is the step of the sequence it sends. With no box free it says "not now".
    """

    reads_only = False

    def __init__(self, step, after: str):
        from openoutsend.leads.models import Outcome

        self.file = step
        self.after = after
        self.closes_as = Outcome.SENT

    def proceed(self, deal, targets: tuple):
        from openoutsend.emails.steps.send import send_step
        from openoutsend.planner import plan_for

        plan = getattr(deal, "plan", None) or plan_for(deal)
        if plan is None or not send_step(deal, self.file, plan):
            return None
        deal.save()
        return _target(targets, self.after)


class Conversing(DealStep):
    """Every step has gone: the conversation over the deal's thread has it."""

    def __init__(self, sequence):
        from openoutsend.leads import conversing
        from openoutsend.machines import Conversation

        self.conversation = Conversation(
            prompt=lambda deal: conversing.prompt_for(deal, sequence),
            rests=_quiet_until(sequence),
            to=COMPLETED,
            then=_close_unresponsive,
            schema=conversing.DealTurn,
            effect=conversing.record_turn,
        )

    def proceed(self, deal, targets: tuple):
        return self.conversation


def _must_stop(deal) -> bool:
    """Whether *deal* is over whatever its state: its address suppressed, or its line
    piped with `stop`."""
    from openoutsend.leads.suppression import is_suppressed

    return deal.stop or is_suppressed(deal.lead.email)


def _stopped_as(deal) -> str:
    """Why a deal that must stop is over, or ``""`` for one that need not.

    Whoever suppressed the address sets the outcome first (`unsubscribed` or
    `undeliverable`); one fired with none is taken for an opt-out.
    """
    from openoutsend.leads.models import Outcome
    from openoutsend.leads.suppression import is_suppressed

    if is_suppressed(deal.lead.email):
        return deal.outcome or Outcome.UNSUBSCRIBED
    return Outcome.STOPPED if deal.stop else ""


def _close_unresponsive(deal) -> None:
    """Record that *deal*'s conversation closed on silence. The address is not suppressed:
    silence is not a request to stop, and a later approach might suit them."""
    from openoutsend.leads.models import Outcome

    deal.outcome = Outcome.UNRESPONSIVE
    deal.save(update_fields=["outcome", "updated_at"])


def _target(targets: tuple, name: str):
    """The state called *name* among a step's *targets*."""
    return next(target for target in targets if target == name)


# ── The states ──


def _states(sequence, converses: bool) -> type[States]:
    """The folder's states: a wait and a send per step, the conversation, the end."""
    last = CONVERSING if converses else COMPLETED

    def declare(namespace):
        for step in sequence.steps:
            number = step.number
            following = sequence.after(number)
            kind = InitialState if number == 0 else State
            namespace[awaiting(number)] = kind(
                f"Waiting for the day {step.path.name} is due.",
                step=Awaiting(sequence, step), tags={WAITING},
            )
            namespace[sending(number)] = State(
                f"{step.path.name} is due, and goes when the send guards have a box free. "
                "A deal with no address rests here until one arrives.",
                step=Sending(step, awaiting(following.number) if following else last), tags={SENDING},
            )
        if converses:
            namespace[CONVERSING] = State(
                "Every step has gone. The model answers whoever wrote back, and reads the "
                "whole thread to do it; silence closes the deal.",
                step=Conversing(sequence),
            )
        namespace[COMPLETED] = FinalState("Over, with the deal's outcome saying why.")

    return types.new_class("DealState", (States,), exec_body=declare)


# ── The edges ──


def _edges(sequence, states, converses: bool) -> list:
    """The folder's graph: where each state's step may send a deal."""
    steps = sequence.steps
    completed = states(COMPLETED)
    still_open = tuple(state for state in states if state is not completed)
    edges = []
    for step in steps:
        following = sequence.after(step.number)
        due = states(sending(step.number))
        edges.append((states(awaiting(step.number)), due))
        if following:
            edges.append((due, states(awaiting(following.number))))
        elif converses:
            edges.append((due, states(CONVERSING)))
    edges += [(states(awaiting(0)), states(awaiting(step.number))) for step in steps[1:]]
    if converses:
        edges.append((states(CONVERSING), states(CONVERSING)))
    edges.append((still_open, completed))
    return edges
