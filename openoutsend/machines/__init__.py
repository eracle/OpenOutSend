"""Two handlers for an `agentic_fsm` machine: `Wait`, which rests for a time, and
`Recheck`, which comes back to someone by email, following up while they are quiet and
answering when they write.

    from datetime import timedelta
    from agentic_fsm import FinalState, InitialState, State, States, check
    from openoutsend.machines import Recheck, MailMachine

    talking = Recheck(
        prompt="They were asked to narrow the request. ...",
        follow_ups=[timedelta(days=2), timedelta(days=4)],   # written by the model
        rests=timedelta(days=5), to="dropped",               # silence after the last one
    )

    @check
    def asked(request):
        if request.withdrawn:
            return Request.dropped       # a fact moves it at once
        return talking                   # otherwise the conversation has it

    class Request(States):
        received = InitialState("Just arrived", handler=ask)    # calls requests.send(...)
        asked    = State("We asked them a question and are talking it over", handler=asked)
        accepted = FinalState("Their answer makes the request workable")
        dropped  = FinalState("They declined, withdrew, or never answered")

    EDGES = [
        (Request.received, Request.asked),
        (Request.asked,    Request.asked),      # an answer or a follow-up: it stays
        (Request.asked,    Request.accepted),
        (Request.asked,    Request.dropped),
    ]
    requests = MailMachine(Request, EDGES, load=get_request, address=lambda r: r.email)

`agentic_fsm` keeps no clock and knows no channel: when a move is tried is its caller's
job. Scheduling and the thread are this package's, built from what the library has: a
handler that says "not now", an edge from a state to itself, and a log whose newest row
for an entity is the move that brought it where it is. Nothing here runs a clock
either: a pass (`progress_all`) moves what is due when it is called, and paces what it
sends with a tag and a limit.

Which conversations exist, what their prompts say and where they lead is declared by
whoever builds the machine. None is declared here.
"""
from openoutsend.machines.machine import MailMachine
from openoutsend.machines.handlers import Recheck, FollowUp, FollowUpTurn, Turn, Wait

__all__ = ["Recheck", "FollowUp", "FollowUpTurn", "MailMachine", "Turn", "Wait"]
