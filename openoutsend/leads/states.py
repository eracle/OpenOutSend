"""Where a deal is in *this* side's funnel — the conversation, not the search.

The states are an `agentic_fsm` `States` class, and the deal's current state is kept by
the library's Django backend (`agentic_fsm_currentstate`, one row per deal, beside an
append-only log of every move), not by a column on `Deal`. The machine that moves them
is `leads/lifecycle.py`.

The finder's states describe finding somebody and optionally resolving an address; they
all end before the first message is written. These three describe what happens after.

**The state says whether it is over; the outcome says why.** `Outcome` stays a column on
`Deal`, written by the step that ends it.

**A state added here is unsendable by default.** The planner's pools name the state they
want (`in_state(READY)`, `in_state(EMAILED)`) rather than excluding the ones they don't,
so a terminal state cannot be re-selected by an exclusion somebody forgot.

The stored values are the labels the old `state` column held (`"Ready to Email"`, …), so
the migration that moved them copied every row as it was, and a query from outside
(the hub's) matches the same strings.
"""
from agentic_fsm import FinalState, InitialState, State, States

#: The machine's name: the `machine` column of the library's tables, and the prefix of a
#: deal at the CLI (`agfsm fire deal:42 give_up`).
MACHINE = "deal"


class DealState(States):
    READY = InitialState(
        "Ingested and not yet written to. A row with no address rests here too: an address "
        "is an enrichment a later run fills in for free.",
        value="Ready to Email",
    )
    EMAILED = State(
        "At least one cold email went out and the conversation is live. A silent lead rests "
        "here between touches; one who answers stays here while the thread runs. Which of "
        "the two a row is, is never stored: it is the thread's newest inbound turn against "
        "its newest outbound one.",
        value="Emailed",
    )
    COMPLETED = FinalState(
        "The conversation reached its end, with an outcome saying how.",
        value="Completed",
    )
