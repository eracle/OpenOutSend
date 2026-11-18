"""The declarations: a state that waits, a state that converses, and the two row markers."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from agentic_fsm import State


class Wait(State):
    """A state an entity rests in for a time.

    ``rests`` is how long: a ``timedelta``, or a function of the entity and the moment
    it entered that returns when the wait runs out (business days, a date the entity
    carries). The moment it entered is the time of the move that brought it here, read
    from the machine's log; nothing is stored on the entity.

    The state's ``timed`` row is refused until then. Its other rows fire at any time.
    """

    def __init__(self, description: str, *, rests: timedelta | Callable[[object, datetime], datetime], **options):
        super().__init__(description, **options)
        if not isinstance(rests, timedelta) and not callable(rests):
            raise TypeError(f"rests= must be a timedelta or a function of the entity and when it entered, got {rests!r}")
        self.rests = rests

    def runs_out_at(self, entity, entered_at: datetime) -> datetime:
        """When *entity*, here since *entered_at*, has rested long enough."""
        if isinstance(self.rests, timedelta):
            return entered_at + self.rests
        return self.rests(entity, entered_at)


class Conversation(Wait):
    """A state an entity rests in across the turns of an email thread.

    ``prompt`` says how to answer: text, or a function of the entity that returns it.
    The state's ``turn`` rows say how the conversation can go on (the row back to the
    state itself: an answer is sent) and how it can end (every other row: nothing is
    sent). It also waits: ``rests`` counts from the last turn, and the ``timed`` row is
    refused while a message is waiting for an answer.
    """

    def __init__(self, description: str, *, prompt: str | Callable[[object], str], rests, **options):
        super().__init__(description, rests=rests, **options)
        if not callable(prompt) and not (isinstance(prompt, str) and prompt.strip()):
            raise TypeError(f"prompt= must be text or a function of the entity, got {prompt!r}")
        self.prompt = prompt

    def prompt_for(self, entity) -> str:
        """How to answer *entity*'s thread."""
        return self.prompt(entity) if callable(self.prompt) else self.prompt


class _Marker:
    """Stands in a row for a step ``MailMachine`` builds for the row's own state."""

    def __init__(self, name: str):
        self.name = name

    def __repr__(self) -> str:
        return self.name


#: The row a ``Wait`` leaves by when its time runs out.
timed = _Marker("timed")

#: The rows of a ``Conversation``'s turn: back to the state to answer, elsewhere to end.
turn = _Marker("turn")
