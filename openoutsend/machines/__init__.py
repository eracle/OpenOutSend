"""Two kinds of state for an `agentic_fsm` machine: one that waits, one that converses.

    from agentic_fsm import FinalState, InitialState, States
    from openoutsend.machines import Conversation, MailMachine, timed, turn

    class Request(States):
        received = InitialState("Just arrived")
        asked    = Conversation("We asked them a question and are talking it over",
                                prompt="They were asked to narrow the request. ...",
                                rests=timedelta(days=5))
        accepted = FinalState("Their answer makes the request workable")
        dropped  = FinalState("They declined, or never answered")

    ROWS = [
        (Request.received, Request.asked,    ask),      # a function that calls requests.send(...)
        (Request.asked,    Request.asked,    turn),     # answer and stay
        (Request.asked,    Request.accepted, turn),
        (Request.asked,    Request.dropped,  turn),
        (Request.asked,    Request.dropped,  timed),    # five quiet days
    ]
    requests = MailMachine(Request, ROWS, load=get_request, address=lambda r: r.email)

`agentic_fsm` keeps no clock and knows no channel: when a move is tried is its caller's
job. Scheduling and the thread are this package's, built from what the library has: a
step that says "not now", a row from a state to itself, and a log whose newest row for
an entity is the move that brought it where it is. Nothing here runs a clock either: a
pass (`progress_all`) moves what is due when it is called.

Which conversations exist, what their prompts say and where they lead is declared by
whoever builds the machine. None is declared here.
"""
from openoutsend.machines.machine import MailMachine
from openoutsend.machines.states import Conversation, Turn, Wait, timed, turn

__all__ = ["Conversation", "MailMachine", "Turn", "Wait", "timed", "turn"]
