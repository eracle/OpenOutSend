"""`MailMachine`: an `agentic_fsm.Machine` whose `timed` and `turn` rows become real steps.

A row is still `(from, to, step)`. Where the step is `timed` or `turn`, the machine
builds the step for the row's own state before the library checks the declaration:

- `timed`, out of a `Wait`: a check named `<state>_ran_out` that says "not now" until
  the wait runs out, counted from the newest row of the entity's log. `due()` runs it.
- `turn`, out of a `Conversation`: one `Decide` named `<state>_turn`. With nothing
  incoming it says "not now". Otherwise the model reads the thread and is offered the
  state's turn rows: the state itself with a message (sent as the answer, under the
  entity's lock, before the move from the state to itself is written), or another
  state (written as that move, nothing sent). A session can take the turn in the
  model's place (`brief`, `fire(answer=)`), and its message is sent the same way.

`progress` stops after a move back to a state it already left, so it makes at most one
turn per call.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime

from agentic_fsm import Decide, Machine, MachineError, States, check
from django.utils import timezone
from pydantic import create_model, model_validator
from pydantic_ai import Agent

from openoutsend.errors import OutsendError
from openoutsend.machines import channel
from openoutsend.machines.states import Conversation, Turn, Wait, timed, turn

DJANGO_BACKEND = "agentic_fsm.backends.django"

TURN_INSTRUCTIONS = """\
You are handling one email conversation. Read the thread, then the instructions for it, and decide the next move.

- To answer and keep the conversation open, set `to` to `{state}` and write `message`: the plain text of the email, with no subject line and no signature.
- To end the conversation, set `to` to one of the other states. Nothing is sent then.

You can only send email in this thread. Never claim to have done anything outside it."""


def now() -> datetime:
    """The time a wait is measured against."""
    return timezone.now()


class MailMachine(Machine):
    """A machine whose entities each have an address, and may wait and converse.

    ``address`` gives an entity's email address and ``key`` its id (``entity.pk`` when
    left out). ``unsubscribe_url`` gives the opt-out link a resend box cannot send
    without. State is kept in this store, by the library's Django backend.
    """

    def __init__(
        self,
        states: type[States],
        transitions: Iterable,
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
        super().__init__(states, self._with_steps(list(transitions)), load=load, backend=backend, name=name)
        _check_every_wait_has_its_rows(states, self.edges)

    # ── The wait ──

    def entered_at(self, entity_id) -> datetime:
        """When the entity came to rest where it is: the time of its newest move."""
        return self.history(entity_id)[-1].at

    # ── The channel ──

    def incoming(self, entity):
        """The message of *entity*'s waiting for an answer, or ``None``.

        Nobody on the suppression list is owed one.
        """
        from openoutsend.leads.suppression import is_suppressed

        if is_suppressed(self.address(entity)):
            return None
        return channel.incoming(self.name, self.key(entity))

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

    def _sendable_address(self, entity) -> str:
        from openoutsend.leads.suppression import is_suppressed

        to_address = self.address(entity)
        if is_suppressed(to_address):
            raise OutsendError(f"{to_address} is on the suppression list")
        return to_address

    # ── Building the steps ──

    def _with_steps(self, rows: list) -> list:
        """*rows* with each `timed` and `turn` marker replaced by its state's step."""
        built: dict = {}
        expanded = []
        for row in rows:
            if not (isinstance(row, tuple) and len(row) == 3 and row[2] in (timed, turn)):
                expanded.append(row)
                continue
            sources, target, marker = row
            for source in sources if isinstance(sources, tuple) else (sources,):
                expanded.append((source, target, self._step_for(built, source, target, marker)))
        return expanded

    def _step_for(self, built: dict, state, target, marker):
        """The one step *marker* stands for out of *state*, built on first sight."""
        declaration = getattr(state, "declaration", None)
        if marker is timed:
            if not isinstance(declaration, Wait):
                raise MachineError(f"{state!r} has a timed row but is not a Wait")
            step, its_target = built.setdefault((state, timed), (self._ran_out(state, target), target))
            if its_target is not target:
                raise MachineError(f"{state.name} has two timed rows; a wait runs out to one state")
            return step
        if not isinstance(declaration, Conversation):
            raise MachineError(f"{state!r} has a turn row but is not a Conversation")
        if (state, turn) not in built:
            built[state, turn] = self._turn(state)
        return built[state, turn]

    def _ran_out(self, state, target) -> Callable:
        """The check that moves an entity out of *state* once it has rested long enough."""
        wait = state.declaration
        answers_first = isinstance(wait, Conversation)

        def ran_out(entity):
            if answers_first and self.incoming(entity) is not None:
                return None
            due_at = wait.runs_out_at(entity, self.entered_at(self.key(entity)))
            if now() < due_at:
                return None
            if wait.then is not None:
                wait.then(entity)
            return target

        ran_out.__name__ = f"{state.name}_ran_out"
        ran_out.__doc__ = f"Leave {state.name} once its wait has run out."
        return ran_out if wait.then is not None else check(ran_out)

    def _turn(self, state) -> Decide:
        """The `Decide` that takes one turn of the conversation *state* is."""
        conversation = state.declaration

        def prompt(entity) -> str | None:
            if self.incoming(entity) is None:
                return None
            so_far = _transcript(channel.turns_of(self.name, self.key(entity)))
            return f"## Everything so far\n\n{so_far}\n\n## Decide now\n\n{conversation.prompt_for(entity)}"

        def send_the_answer(entity, decision):
            if decision.to == state:
                self.answer(entity, decision.message, prompt=conversation.prompt_for(entity))
            if conversation.effect is not None:
                conversation.effect(entity, decision)

        agent = Agent(
            None,
            output_type=_turn_schema(state, conversation.schema),
            instructions=TURN_INSTRUCTIONS.format(state=state.value),
            defer_model_check=True,
        )
        return _ModelTurn(name=f"{state.name}_turn", prompt=prompt, agent=agent, effect=send_the_answer)


class _ModelTurn(Decide):
    """A `Decide` asked of this install's own model (`core/llm.py`), chosen when it runs."""

    @property
    def decided_by(self) -> str:
        from openoutsend.core.llm import get_llm_model

        model = get_llm_model()
        return f"llm:{model.system}:{model.model_name}"

    def ask(self, prompt, entity, schema):
        from openoutsend.core.llm import get_llm_model, run_agent_sync

        run = self.agent.run(prompt, deps=entity, output_type=schema, model=get_llm_model(),
                             model_settings={"timeout": 60})
        return run_agent_sync(run).output


# ── Helpers ──


def _turn_schema(state, schema: type[Turn]) -> type[Turn]:
    """*schema*, refusing an answer that stays in *state* with nothing to send."""

    def staying_needs_a_message(self):
        if self.to == state and not (self.message or "").strip():
            raise ValueError(f"message is required when `to` is {state.value}")
        return self

    return create_model(
        schema.__name__,
        __base__=schema,
        __validators__={"staying_needs_a_message": model_validator(mode="after")(staying_needs_a_message)},
    )


def _transcript(turns: list) -> str:
    from openoutsend.core.agents.history import transcript

    return transcript(turns, now())


def _check_every_wait_has_its_rows(states: type[States], edges: list):
    """A wait with no timed row never runs out; a conversation with no way to stay can't answer."""
    for state in states:
        declaration = state.declaration
        leaving = [edge for edge in edges if edge.from_state is state]
        if isinstance(declaration, Wait) and not any(_named(edge, f"{state.name}_ran_out") for edge in leaving):
            raise MachineError(f"{state.name} is a Wait with no timed row")
        if isinstance(declaration, Conversation) and not any(
            edge.to_state is state and _named(edge, f"{state.name}_turn") for edge in leaving
        ):
            raise MachineError(f"{state.name} is a Conversation with no turn row back to itself")


def _named(edge, name: str) -> bool:
    step = edge.step
    return (step.name if isinstance(step, Decide) else step.__name__) == name
