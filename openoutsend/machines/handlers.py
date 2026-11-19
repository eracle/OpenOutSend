"""The handlers: one that waits, one that converses, and what a conversation logs."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timedelta

from agentic_fsm import Decide, Decision, States, Handler
from pydantic import Field, create_model, model_validator
from pydantic_ai import Agent

from openoutsend.errors import OutsendError

TURN_INSTRUCTIONS = """\
You are handling one email conversation. Read the thread, then the instructions for it, and decide the next move.

- To answer and keep the conversation open, set `to` to `{state}` and write `message`: the plain text of the email, with no subject line and no signature.
- To end the conversation, set `to` to one of the other states. Nothing is sent then.

You can only send email in this thread. Never claim to have done anything outside it."""

FOLLOW_UP_INSTRUCTIONS = """\
You are handling one email conversation. Nothing has arrived since the last message in the thread. Read the thread, then the instructions for it, and write the next follow-up.

Set `to` to `{state}` and write `message`: the plain text of the email, with no subject line and no signature.

You can only send email in this thread. Never claim to have done anything outside it."""


class Turn(Decision):
    """One turn of a conversation: stay with a message, or leave.

    A conversation whose turn carries more (why it ended) declares a subclass with the
    fields it adds (``Conversation(schema=)``); the whole turn is logged with its move.
    """

    to: States
    message: str | None = Field(
        default=None,
        description="The email to send, when `to` keeps the conversation open. Leave empty when it ends.",
    )


class FollowUpTurn(Decision):
    """A follow-up a conversation sent while nothing happened: its text and which one it was.

    Logged with its move from the state to itself, which is how the next one is counted.
    """

    to: States
    message: str
    follow_up: int


class FollowUp:
    """One follow-up of a conversation: how long after the last move, and what it says.

    ``after`` is a ``timedelta``, or a function of the entity and the time of its last
    move that returns when the follow-up is due. ``text`` is the email as written, or a
    function of the entity that returns it; left out, the model writes it from the
    conversation's prompt and the thread.
    """

    def __init__(self, after, text: str | Callable[[object], str] | None = None):
        _check_gap("after", after)
        if text is not None and not callable(text) and not (isinstance(text, str) and text.strip()):
            raise TypeError(f"text= must be text or a function of the entity, got {text!r}")
        self.after = after
        self.text = text

    def text_for(self, entity) -> str:
        return self.text(entity) if callable(self.text) else self.text


class Wait(Handler):
    """A handler that leaves an entity resting for a time.

    ``rests`` is how long: a ``timedelta``, or a function of the entity and the moment
    it entered that returns when the wait runs out (business days, a date the entity
    carries). The moment it entered is the time of the move that brought it here, read
    from the machine's log; nothing is stored on the entity.

    ``to`` is the state the entity goes to then, as its stored value or the state
    itself. It may be left out when the state has one edge to another state.

    ``then`` is a function of the entity, run with the move when the wait runs out,
    under the entity's lock: what the entity records about having waited in vain.

    The handler only reads, so ``due()`` asks it; ``then`` runs as its effect.
    """

    reads_only = True

    def __init__(self, *, rests: timedelta | Callable[[object, datetime], datetime],
                 to=None, then: Callable[[object], None] | None = None):
        _check_gap("rests", rests)
        if then is not None and not callable(then):
            raise TypeError(f"then= must be a function of the entity, got {then!r}")
        self.rests = rests
        self.to = to
        self.then = then

    def run(self, entity, targets: tuple):
        from openoutsend.machines.machine import now, running

        rest = running().rest_of(entity)
        if now() < _due(self.rests, entity, rest.since):
            return None
        return self.target(rest.state, targets)

    def effect(self, entity, result) -> None:
        if self.then is not None:
            self.then(entity)

    def target(self, state, targets: tuple):
        """Where the entity goes from *state* when the time runs out."""
        if self.to is not None:
            for target in targets:
                if self.to in (target, target.value, target.name):
                    return target
            raise OutsendError(f"{state.value} has no edge to {self.to!r}, where its wait runs out")
        others = [target for target in targets if target is not state]
        if len(others) != 1:
            raise OutsendError(
                f"{state.value} has edges to {len(others)} other states: say where its wait runs out with to=")
        return others[0]


class Conversation(Wait):
    """A handler that holds an email thread with the entity, and may follow up while it is quiet.

    In this order, each time it runs:

    1. A message is waiting: the model answers and the entity stays (a move from the
       state to itself, its message sent), or the model moves it to another of the
       state's targets (nothing is sent).
    2. A follow-up is due: it is sent in the entity's newest thread and logged as a
       move from the state to itself. Each of ``follow_ups`` is a ``FollowUp``, or its
       ``after`` alone for one the model writes. Which is next is counted from the log,
       and each is due its time after the entity's last move, so an answered reply
       delays the next follow-up and does not use one up.
    3. Every follow-up is sent and ``rests`` has run out since the last move: the
       entity leaves to ``to``, and ``then`` runs.

    ``prompt`` says how to answer and how to write a follow-up: text, or a function of
    the entity that returns it. ``schema`` is the turn's own ``Turn`` subclass, for a
    conversation whose turn says more than where it goes. ``effect`` is a function of
    the entity and the turn or follow-up, run after its message is sent and before its
    move is written.

    The state it is given to needs an edge to itself. A function may hand over to one
    (``return nudging``) after checking its facts; the function is then marked
    ``@check``, so a session can take the turn (``brief``, ``fire(answer=)``).
    """

    def __init__(self, *, prompt: str | Callable[[object], str], rests, to=None, then=None,
                 follow_ups: Iterable = (), schema: type[Turn] = Turn,
                 effect: Callable[[object, Decision], None] | None = None):
        super().__init__(rests=rests, to=to, then=then)
        if not callable(prompt) and not (isinstance(prompt, str) and prompt.strip()):
            raise TypeError(f"prompt= must be text or a function of the entity, got {prompt!r}")
        if not (isinstance(schema, type) and issubclass(schema, Turn)):
            raise TypeError(f"schema= must be a subclass of Turn, got {schema!r}")
        if effect is not None and not callable(effect):
            raise TypeError(f"effect= must be a function of the entity and the turn, got {effect!r}")
        self.prompt = prompt
        self.follow_ups = tuple(item if isinstance(item, FollowUp) else FollowUp(item) for item in follow_ups)
        self.schema = schema
        self.on_turn = effect
        self._turns: dict = {}

    def prompt_for(self, entity) -> str:
        """How to answer *entity*'s thread."""
        return self.prompt(entity) if callable(self.prompt) else self.prompt

    def run(self, entity, targets: tuple):
        from openoutsend.machines.machine import now, running

        machine = running()
        rest = machine.rest_of(entity)
        if machine.incoming(entity) is not None:
            return self._turn(rest.state, None)
        sent = rest.follow_ups_sent
        if sent < len(self.follow_ups) and not machine.suppressed(entity):
            follow_up = self.follow_ups[sent]
            if now() < _due(follow_up.after, entity, rest.since):
                return None
            if follow_up.text is None:
                return self._turn(rest.state, sent + 1)
            return FollowUpTurn(to=rest.state, message=follow_up.text_for(entity), follow_up=sent + 1)
        if now() < _due(self.rests, entity, rest.since):
            return None
        return self.target(rest.state, targets)

    def effect(self, entity, result) -> None:
        """What goes with a result this handler returned itself: a written follow-up is
        sent, a wait that ran out runs ``then``."""
        if isinstance(result, FollowUpTurn):
            self._sent(entity, result, result.to)
        elif self.then is not None:
            self.then(entity)

    # ── The model's part ──

    def _turn(self, state, follow_up: int | None) -> Decide:
        """The `Decide` that answers in *state*, or writes its follow-up number *follow_up*."""
        if (state, follow_up) not in self._turns:
            instructions = FOLLOW_UP_INSTRUCTIONS if follow_up else TURN_INSTRUCTIONS
            agent = Agent(
                None,
                output_type=_staying_needs_a_message(state, Turn if follow_up else self.schema),
                instructions=instructions.format(state=state.value),
                defer_model_check=True,
            )
            self._turns[state, follow_up] = ModelTurn(
                state, follow_up, prompt=lambda entity: self._model_prompt(entity, follow_up),
                agent=agent, effect=lambda entity, decision: self._sent(entity, decision, state),
            )
        return self._turns[state, follow_up]

    def _model_prompt(self, entity, follow_up: int | None) -> str:
        from openoutsend.machines.machine import running

        asked = self.prompt_for(entity)
        if follow_up:
            asked += (f"\n\nNothing has arrived since the last message above. "
                      f"Write follow-up {follow_up} of {len(self.follow_ups)}.")
        return f"## Everything so far\n\n{running().transcript(entity)}\n\n## Decide now\n\n{asked}"

    def _sent(self, entity, decision, state) -> None:
        """Send what *decision* wrote in *state*, then run the conversation's own effect."""
        from openoutsend.machines.machine import running

        machine = running()
        prompt = self.prompt_for(entity)
        if isinstance(decision, FollowUpTurn):
            machine.follow_up(entity, decision.message, prompt=prompt)
        elif decision.to == state:
            machine.answer(entity, decision.message, prompt=prompt)
        if self.on_turn is not None:
            self.on_turn(entity, decision)


class ModelTurn(Decide):
    """A conversation's `Decide`, asked of this install's own model (`core/llm.py`),
    chosen when it runs. One that writes a follow-up is offered only its own state."""

    def __init__(self, state, follow_up: int | None, **options):
        super().__init__(**options)
        self.state = state
        self.follow_up = follow_up

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

    def run(self, entity, targets: tuple):
        return self._logged(super().run(entity, self._offered(targets)))

    def parse(self, answer, targets: tuple):
        return self._logged(super().parse(answer, self._offered(targets)))

    def brief(self, entity, state, targets: tuple):
        return super().brief(entity, state, self._offered(targets))

    def _offered(self, targets: tuple) -> tuple:
        return (self.state,) if self.follow_up else targets

    def _logged(self, decision):
        if decision is None or not self.follow_up:
            return decision
        return FollowUpTurn(to=decision.to, message=decision.message, follow_up=self.follow_up)


# ── Helpers ──


def _check_gap(name: str, gap) -> None:
    if not isinstance(gap, timedelta) and not callable(gap):
        raise TypeError(
            f"{name}= must be a timedelta or a function of the entity and when it last moved, got {gap!r}")


def _due(gap, entity, since: datetime) -> datetime:
    """When *gap* has passed for *entity*, resting since *since*."""
    return since + gap if isinstance(gap, timedelta) else gap(entity, since)


def _staying_needs_a_message(state, schema: type[Turn]) -> type[Turn]:
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
