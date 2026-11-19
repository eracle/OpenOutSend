"""`MailMachine`: an `agentic_fsm.Machine` whose entities have an address, so its steps
may wait and converse.

A `Wait` or a `Conversation` (`machines/steps.py`) is given to a state as its step, or
handed over to by a state's function. Either way it runs with nothing but the entity,
so it asks the machine that is running it (`running()`) for the rest: since when the
entity has rested where it is, what is waiting in its threads, and the send.

`progress` stops after a move back to a state it already left, so it makes at most one
turn or follow-up per call.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime

from agentic_fsm import Machine, MachineError, States
from django.utils import timezone

from openoutsend.errors import OutsendError
from openoutsend.machines import channel
from openoutsend.machines.steps import Conversation, Wait

DJANGO_BACKEND = "agentic_fsm.backends.django"

_RUNNING: ContextVar[MailMachine | None] = ContextVar("openoutsend_running_machine", default=None)


def now() -> datetime:
    """The time a wait is measured against."""
    return timezone.now()


def running() -> MailMachine:
    """The machine whose call is running the step that asks."""
    machine = _RUNNING.get()
    if machine is None:
        raise OutsendError("a Wait or a Conversation runs only as a step of a MailMachine")
    return machine


@dataclass(frozen=True)
class Rest:
    """Where an entity rests, read from its log."""

    state: States
    since: datetime
    """The time of its newest move: the clock every wait and follow-up counts from."""
    follow_ups_sent: int
    """The follow-ups sent since it entered the state."""


class MailMachine(Machine):
    """A machine whose entities each have an address, and may wait and converse.

    ``address`` gives an entity's email address and ``key`` its id (``entity.pk`` when
    left out). ``unsubscribe_url`` gives the opt-out link a resend box cannot send
    without. State is kept in this store, by the library's Django backend.
    """

    def __init__(
        self,
        states: type[States],
        edges: Iterable,
        *,
        load: Callable[[str], object],
        address: Callable[[object], str],
        key: Callable[[object], object] = lambda entity: entity.pk,
        unsubscribe_url: Callable[[object], str] = lambda entity: "",
        backend=DJANGO_BACKEND,
        name: str | None = None,
    ):
        self.address = address
        self.key = key
        self.unsubscribe_url = unsubscribe_url
        super().__init__(states, edges, load=load, backend=backend, name=name)
        _check_mail_steps(states, self._targets)

    # ── Running ──

    @contextmanager
    def _as_running(self):
        token = _RUNNING.set(self)
        try:
            yield
        finally:
            _RUNNING.reset(token)

    def fire(self, entity_id, **options):
        with self._as_running():
            return super().fire(entity_id, **options)

    def brief(self, entity_id, **options):
        with self._as_running():
            return super().brief(entity_id, **options)

    def progress(self, entity_id):
        with self._as_running():
            return super().progress(entity_id)

    def progress_all(self, tag: str | None = None, *, limit: int | None = None):
        with self._as_running():
            return super().progress_all(tag, limit=limit)

    def due(self, tag: str | None = None):
        with self._as_running():
            return super().due(tag)

    # ── The wait ──

    def entered_at(self, entity_id) -> datetime:
        """When the entity came to rest where it is: the time of its newest move."""
        return self.history(entity_id)[-1].at

    def rest_of(self, entity) -> Rest:
        """Where *entity* rests, since when, and the follow-ups sent since it got there."""
        moves = self.history(self.key(entity))
        newest = moves[-1]
        sent = 0
        for move in reversed(moves):
            if move.from_state != move.to_state:
                break
            sent += "follow_up" in (move.decision or {})
        return Rest(self.states(newest.to_state), newest.at, sent)

    # ── The channel ──

    def suppressed(self, entity) -> bool:
        """Whether *entity*'s address is on the suppression list."""
        from openoutsend.leads.suppression import is_suppressed

        return is_suppressed(self.address(entity))

    def incoming(self, entity):
        """The message of *entity*'s waiting for an answer, or ``None``.

        Nobody on the suppression list is owed one.
        """
        if self.suppressed(entity):
            return None
        return channel.incoming(self.name, self.key(entity))

    def transcript(self, entity) -> str:
        """Every turn in *entity*'s threads, as the model reads them."""
        from openoutsend.core.agents.history import transcript

        return transcript(channel.turns_of(self.name, self.key(entity)), now())

    def send(self, entity, subject: str, body: str, *, mailbox, prompt: str = ""):
        """Open a thread kept against *entity* with one email from *mailbox*; returns its row.

        For a step to call, so the send commits with the step's move. ``prompt`` is what
        the email was written from, recorded for the history a later turn reads.
        """
        to_address = self._sendable_address(entity)
        return channel.open_thread(
            self.name, self.key(entity), mailbox, to_address, subject, body,
            prompt=prompt, unsubscribe_url=self.unsubscribe_url(entity),
        )

    def answer(self, entity, body: str, *, prompt: str = ""):
        """Send *body* as the answer to *entity*'s incoming message, in its thread."""
        self._sendable_address(entity)
        message = channel.incoming(self.name, self.key(entity))
        if message is None:
            raise OutsendError(f"{self.name}:{self.key(entity)} has no message waiting for an answer")
        return channel.answer(message, body, prompt=prompt, unsubscribe_url=self.unsubscribe_url(entity))

    def follow_up(self, entity, body: str, *, prompt: str = ""):
        """Send *body* in *entity*'s newest thread, with nothing of theirs to answer."""
        to_address = self._sendable_address(entity)
        return channel.follow_up(self.name, self.key(entity), to_address, body, prompt=prompt,
                                 unsubscribe_url=self.unsubscribe_url(entity))

    def _sendable_address(self, entity) -> str:
        to_address = self.address(entity)
        if self.suppressed(entity):
            raise OutsendError(f"{to_address} is on the suppression list")
        return to_address


# ── Build-time checks ──


def _check_mail_steps(states: type[States], targets: dict) -> None:
    """A wait must know where it runs out to; a conversation needs a way to stay.

    Only the steps given to a state are seen here. One a function hands over to is
    checked when it runs.
    """
    for state in states:
        step = state.step
        if not isinstance(step, Wait):
            continue
        if isinstance(step, Conversation) and state not in targets[state]:
            raise MachineError(f"{state.name} holds a Conversation but has no edge back to itself")
        try:
            step.target(state, targets[state])
        except OutsendError as error:
            raise MachineError(str(error)) from None
