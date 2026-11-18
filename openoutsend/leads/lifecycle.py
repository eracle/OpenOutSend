"""The deal machine: a sequence folder, read as a machine of waits, sends and one conversation.

    agfsm --machine openoutsend.leads.lifecycle:cold show

Every folder becomes the same shape, built when it is asked for (`machine_for`), one
machine per folder under the folder's name (`leads/states.machine_name`):

    awaiting_E<k>   a wait: step k's gap, after the deal's `start_at` for the step it
                    opens with, in business days after step k-1 went for the rest
    sending_E<k>    step k is due and waits for a box the send guards say is free
    conversing      after the last step: the model answers whoever wrote back, and
                    silence closes the deal
    completed       over, with `Deal.outcome` saying why

A deal's state is kept by `agentic_fsm` (its Django backend's two tables, in this
store): one current-state row per deal and an append-only log of every move, with the
step that made it. Nothing writes a deal's state except a step of its machine, so a
folder's rows are its whole graph, and a move's time in the log is what the next wait
counts from. Nothing about timing is kept on the deal.

**Neither half of a step decides.** The wait runs out, the send sends: an email
template as written, a prompt template through the model, and the deal moves on either
way. A reply never stops or skips a step. A deal that wrote back is answered in
`conversing`, where the model reads the whole thread.

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

from agentic_fsm import FinalState, State, States, check

from openoutsend.leads.states import (
    COMPLETED,
    CONVERSING,
    SENDING,
    WAITING,
    awaiting,
    machine_name,
    sending,
)

END_SUPPRESSED = "end_suppressed"
CLOSE_MISSED = "close_missed"
OPEN_LATER = "open_later"
TURN = f"{CONVERSING}_turn"
RAN_QUIET = f"{CONVERSING}_ran_out"


class Sending(State):
    """A deal whose ``step`` is due: it rests here until a box may send it."""

    def __init__(self, description: str, *, step, **options):
        super().__init__(description, **options)
        self.step = step


# ── Finding a deal's machine ──


def machine_of(deal):
    """The machine *deal* moves through: its sequence folder's."""
    from openoutsend.core.sequences import for_deal

    return machine_for(for_deal(deal))


@lru_cache(maxsize=64)
def machine_for(sequence):
    """The machine a sequence folder is, built once per folder as it reads now."""
    from openoutsend.machines import MailMachine

    states = _states(sequence)
    return MailMachine(
        states, _rows(sequence, states),
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


# ── The states ──


def _states(sequence) -> type[States]:
    """The folder's states: a wait and a send per step, the conversation, the end."""
    from openoutsend.leads import conversing
    from openoutsend.machines import Conversation, Wait

    def declare(namespace):
        for step in sequence.steps:
            number = step.number
            namespace[awaiting(number)] = Wait(
                f"Waiting for the day {step.path.name} is due.",
                rests=_step_due(sequence, step), initial=number == 0, tags={WAITING},
            )
            namespace[sending(number)] = Sending(
                f"{step.path.name} is due, and goes when the send guards have a box free. "
                "A deal with no address rests here until one arrives.",
                step=step, tags={SENDING},
            )
        namespace[CONVERSING] = Conversation(
            "Every step has gone. The model answers whoever wrote back, and reads the "
            "whole thread to do it; silence closes the deal.",
            prompt=lambda deal: conversing.prompt_for(deal, sequence),
            rests=_quiet_until(sequence),
            then=_close_unresponsive,
            schema=conversing.DealTurn,
            effect=conversing.record_turn,
        )
        namespace[COMPLETED] = FinalState("Over, with the deal's outcome saying why.")

    return types.new_class("DealState", (States,), exec_body=declare)


def _close_unresponsive(deal) -> None:
    """Record that *deal*'s conversation closed on silence. The address is not suppressed:
    silence is not a request to stop, and a later approach might suit them."""
    from openoutsend.leads.models import Outcome

    deal.outcome = Outcome.UNRESPONSIVE
    deal.save(update_fields=["outcome", "updated_at"])


# ── The rows ──


def _rows(sequence, states) -> list:
    """The folder's graph. The order of a state's rows is the order they are tried in."""
    from openoutsend.machines import timed, turn

    steps = sequence.steps
    first, completed, in_conversation = states(awaiting(0)), states(COMPLETED), states(CONVERSING)
    still_open = tuple(state for state in states if state is not completed)
    rows = [(still_open, completed, _end_suppressed(completed))]
    if len(steps) > 1:
        open_later = _open_later(sequence, states)
        rows += [(first, states(awaiting(step.number)), open_later) for step in steps[1:]]
    rows.append((first, completed, _close_missed(sequence, completed)))
    for step in steps:
        following = sequence.after(step.number)
        after = states(awaiting(following.number)) if following else in_conversation
        waiting, due = states(awaiting(step.number)), states(sending(step.number))
        rows.append((waiting, due, timed))
        rows.append((due, after, _send(step, after)))
    rows += [
        (in_conversation, in_conversation, turn),
        (in_conversation, completed, turn),
        (in_conversation, completed, timed),
    ]
    return rows


def _end_suppressed(completed):
    def end_suppressed(deal):
        """End an open deal whose address is suppressed, or whose line was piped with `stop`.

        Sends nothing. Whoever suppressed the address sets the outcome first
        (`unsubscribed` or `undeliverable`); one fired with none is taken for an opt-out.
        """
        from openoutsend.leads.models import Outcome
        from openoutsend.leads.suppression import is_suppressed

        if is_suppressed(deal.lead.email):
            deal.outcome = deal.outcome or Outcome.UNSUBSCRIBED
        elif deal.stop:
            deal.outcome = Outcome.STOPPED
        else:
            return None
        deal.save()
        return completed

    return end_suppressed


def _open_later(sequence, states):
    @check
    def open_later(deal):
        """Skip the steps already past their grace when the deal arrived: wait for the
        first one that was not."""
        opening = opening_step(deal, sequence)
        if opening is None or opening[0].number == 0:
            return None
        return states(awaiting(opening[0].number))

    return open_later


def _close_missed(sequence, completed):
    def close_missed(deal):
        """End a deal every step of whose sequence was past its grace when it arrived."""
        from openoutsend.leads.models import Outcome

        if opening_step(deal, sequence) is not None:
            return None
        deal.outcome = Outcome.MISSED
        deal.save()
        return completed

    return close_missed


def _send(step, after):
    def send(deal):
        from openoutsend.emails.steps.send import send_step
        from openoutsend.planner import plan_for

        plan = getattr(deal, "plan", None) or plan_for(deal)
        if plan is None or not send_step(deal, step, plan):
            return None
        deal.save()
        return after

    send.__name__ = send_name(step.number)
    send.__doc__ = f"Send {step.path.name}: as written, or through the model when it is a prompt."
    return send


def send_name(number: int) -> str:
    """The name of the step that sends step *number*."""
    return f"send_E{number}"
