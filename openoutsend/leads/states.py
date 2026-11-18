"""The names of a deal's states, and of the machine that keeps them.

A deal's state is kept by `agentic_fsm`'s Django backend (`agentic_fsm_currentstate`,
one row per deal, beside an append-only log of every move), not by a column on `Deal`.
Every sequence folder is its own machine (`leads/lifecycle.py`), named after the
folder, and every one has the same shape:

    awaiting_E<k>   waiting for step k's day            (a wait)
    sending_E<k>    step k is due, waiting for a box    (the send guards)
    conversing      every step has gone                 (a conversation over the thread)
    completed       over, with `Deal.outcome` saying why

**The state says where the deal is; the outcome says why it ended.** `Outcome` stays a
column on `Deal`, written by the step that ends it.

This module holds only names, so the models can import it. The states themselves are
declared per folder, where the machine is built.
"""
from __future__ import annotations

CONVERSING = "conversing"
COMPLETED = "completed"

#: The queues a pass works (`send_pass.py`): the tags of a deal machine's states.
WAITING = "waiting"
SENDING = "sending"


def awaiting(number: int) -> str:
    """The state of a deal waiting for step *number*'s day."""
    return f"awaiting_E{number}"


def sending(number: int) -> str:
    """The state of a deal whose step *number* is due and waits for a free box."""
    return f"sending_E{number}"


def machine_name(sequence: str) -> str:
    """The name the deals of *sequence* are stored under: the `machine` column of the
    library's tables, and the prefix of a deal at the CLI (`agfsm fire deal-cold:42 …`)."""
    return f"deal-{sequence}"
