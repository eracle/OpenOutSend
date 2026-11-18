"""A wait and a conversation over a thread, on a machine that has no deal in it.

The entity is a request somebody sent in: a plain object with an id and an address,
kept in a dict. It is asked a question by email, rests in a conversation, and is moved
by the answer, by a quiet week, or by a session standing in for the model.
"""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from agentic_fsm import FinalState, InitialState, MachineError, State, States, TransitionError
from django.utils import timezone
from pydantic_ai.models.test import TestModel

from openoutsend.emails.models import Message, Thread
from openoutsend.errors import OutsendError
from openoutsend.leads.suppression import suppress_email
from openoutsend.machines import Conversation, MailMachine, Wait, timed, turn
from openoutsend.tests.emails import maillog

pytestmark = pytest.mark.django_db

ASKER = "ann@acme.com"
HOW_TO_ANSWER = "They were asked which country they sell in. Accept once they name one."


class Request(States):
    received = InitialState("Just arrived")
    held = Wait("Held a day before anything is asked", rests=timedelta(days=1))
    asking = State("Due its question")
    asked = Conversation("Asked a question, talking it over", prompt=HOW_TO_ANSWER, rests=timedelta(days=5))
    accepted = FinalState("Their answer makes the request workable")
    dropped = FinalState("They declined, or never answered")


@pytest.fixture
def box(operator):
    return maillog.mailbox("desk@corp.com")


@pytest.fixture
def table():
    return {"1": SimpleNamespace(pk=1, email=ASKER)}


@pytest.fixture
def requests(box, table):
    def hold(request):
        return Request.held

    def ask(request):
        machine.send(request, "One question", "Which country do you sell in?", mailbox=box)
        return Request.asked

    # fmt: off
    rows = [
        (Request.received, Request.held,     hold),
        (Request.held,     Request.asking,   timed),
        (Request.asking,   Request.asked,    ask),
        (Request.asked,    Request.asked,    turn),
        (Request.asked,    Request.accepted, turn),
        (Request.asked,    Request.dropped,  turn),
        (Request.asked,    Request.dropped,  timed),
    ]
    # fmt: on
    machine = MailMachine(Request, rows, load=table.__getitem__, address=lambda request: request.email,
                          name="request")
    return machine


@pytest.fixture(autouse=True)
def nothing_leaves_the_machine():
    with patch("openoutsend.emails.sender._deliver") as deliver:
        yield deliver


def later(**gap):
    """The clock a wait is measured against, set ahead by *gap*."""
    return patch("openoutsend.machines.machine.now", return_value=timezone.now() + timedelta(**gap))


def backdate(requests, **gap):
    """Move the entity's newest log row back by *gap*: it has rested that long already."""
    from agentic_fsm.backends.django.models import LogEntry

    newest = LogEntry.objects.filter(machine=requests.name, entity="1").order_by("pk").last()
    newest.at -= timedelta(**gap)
    newest.save()


def asked(requests, box) -> Thread:
    """Request 1 resting in `asked`, its question sent; returns the thread."""
    requests.start(1)
    requests.fire(1, "hold")
    with later(days=1):
        requests.fire(1, "held_ran_out")
    requests.fire(1, "ask")
    return Thread.objects.get(machine="request", entity="1")


def they_wrote(thread, body="Italy, mostly.", **options) -> Message:
    return maillog.inbound(thread.mailbox, thread=thread, sender=ASKER, body=body, **options)


def model_saying(**fields):
    return patch("openoutsend.core.llm.get_llm_model", return_value=TestModel(custom_output_args=fields))


# ── A wait ──


def test_a_timed_row_is_refused_until_the_wait_runs_out(requests):
    requests.start(1)
    requests.fire(1, "hold")

    with later(hours=23):
        assert requests.fire(1, "held_ran_out") is None
        assert requests.due() == []
        assert requests.progress_all() == []
    with later(hours=25):
        assert requests.due() == ["1"]
        (move,) = requests.progress_all()

    assert (move.step, move.to_state, move.decided_by) == ("held_ran_out", "asking", "code")


def test_a_wait_counts_from_the_move_that_brought_the_entity_there(requests):
    requests.start(1)
    backdate(requests, days=3)
    requests.fire(1, "hold")

    assert requests.entered_at(1) == requests.history(1)[-1].at
    assert requests.fire(1, "held_ran_out") is None


def test_a_wait_may_say_when_it_runs_out_from_the_entity(box, table):
    class Parcel(States):
        packed = InitialState("Packed")
        stored = Wait("Stored until its pickup date", rests=lambda parcel, entered_at: parcel.pickup_at)
        gone = FinalState("Picked up")

    def store(parcel):
        return Parcel.stored

    rows = [(Parcel.packed, Parcel.stored, store), (Parcel.stored, Parcel.gone, timed)]
    parcels = MailMachine(Parcel, rows, load=table.__getitem__, address=lambda parcel: parcel.email)
    table["1"].pickup_at = timezone.now() + timedelta(days=2)
    parcels.start(1)
    parcels.fire(1, store)

    with later(days=1):
        assert parcels.fire(1, "stored_ran_out") is None
    with later(days=2, minutes=1):
        assert parcels.fire(1, "stored_ran_out").to_state == "gone"


# ── Asking by email ──


def test_a_step_opens_a_thread_kept_against_the_entity(requests, box, nothing_leaves_the_machine):
    thread = asked(requests, box)

    assert requests.state_of(1) is Request.asked
    (sent,) = thread.messages.all()
    assert (sent.to_address, sent.subject, sent.body_text) == (ASKER, "One question", "Which country do you sell in?")
    assert nothing_leaves_the_machine.called


def test_nobody_suppressed_is_written_to(requests, box):
    suppress_email(ASKER, reason="asked to stop")

    with pytest.raises(OutsendError, match="suppression list"):
        requests.send(requests.load("1"), "One question", "Which country?", mailbox=box)

    assert not Thread.objects.exists()


# ── A conversation ──


def test_with_nothing_incoming_the_turn_says_not_now(requests, box):
    asked(requests, box)

    assert requests.incoming(requests.load("1")) is None
    assert requests.fire(1, "asked_turn") is None


def test_a_bounce_or_an_out_of_office_is_not_incoming(requests, box):
    thread = asked(requests, box)
    they_wrote(thread, kind="auto_reply")
    they_wrote(thread, kind="bounce")

    assert requests.fire(1, "asked_turn") is None


def test_the_model_answers_and_the_turn_is_a_move_from_the_state_to_itself(requests, box):
    thread = asked(requests, box)
    reply = they_wrote(thread)

    with model_saying(to="asked", message="Thanks. Which regions of Italy?"):
        moves = requests.progress(1)

    (move,) = moves
    assert (move.step, move.from_state, move.to_state) == ("asked_turn", "asked", "asked")
    assert move.decided_by == "llm:test:test"
    assert move.decision == {"to": "asked", "message": "Thanks. Which regions of Italy?"}
    answer = thread.messages.order_by("pk").last()
    assert answer.is_outbound and answer.body_text == "Thanks. Which regions of Italy?"
    assert (answer.to_address, answer.subject) == (ASKER, "Re: One question")
    assert answer.in_reply_to == reply.message_id
    assert answer.prompt == HOW_TO_ANSWER
    assert requests.incoming(requests.load("1")) is None


def test_the_model_reads_the_whole_thread_and_the_states_prompt(requests, box):
    thread = asked(requests, box)
    they_wrote(thread, body="Italy, mostly.")

    brief = requests.brief(1, "asked_turn")

    assert "Which country do you sell in?" in brief.prompt
    assert "THEY WROTE:\nItaly, mostly." in brief.prompt
    assert brief.prompt.endswith(HOW_TO_ANSWER)
    assert "set `to` to `asked`" in brief.instructions[0]
    assert [value for value, _ in brief.choices] == ["asked", "accepted", "dropped"]


def test_the_model_leaves_by_one_of_the_states_rows_and_sends_nothing(requests, box):
    thread = asked(requests, box)
    they_wrote(thread)

    with model_saying(to="accepted"):
        (move,) = requests.progress(1)

    assert move.to_state == "accepted"
    assert thread.messages.filter(direction="out").count() == 1


def test_a_session_takes_the_turn_and_its_message_is_sent(requests, box):
    thread = asked(requests, box)
    they_wrote(thread)

    move = requests.fire(1, "asked_turn", answer={"to": "asked", "message": "Which regions?"})

    assert (move.to_state, move.decided_by) == ("asked", "agent")
    assert thread.messages.order_by("pk").last().body_text == "Which regions?"


def test_staying_with_nothing_to_send_is_refused(requests, box):
    thread = asked(requests, box)
    they_wrote(thread)

    with pytest.raises(TransitionError, match="message is required"):
        requests.fire(1, "asked_turn", answer={"to": "asked"})

    assert len(requests.history(1)) == 4


def test_a_send_that_raises_writes_no_move(requests, box, nothing_leaves_the_machine):
    thread = asked(requests, box)
    they_wrote(thread)
    nothing_leaves_the_machine.side_effect = OSError("connection reset")

    with pytest.raises(Exception, match="connection reset"):
        requests.fire(1, "asked_turn", answer={"to": "asked", "message": "Which regions?"})

    assert requests.state_of(1) is Request.asked
    assert len(requests.history(1)) == 4


# ── A conversation that goes quiet ──


def test_a_quiet_conversation_leaves_by_time_counted_from_its_last_turn(requests, box):
    thread = asked(requests, box)
    backdate(requests, days=4)
    they_wrote(thread)
    requests.fire(1, "asked_turn", answer={"to": "asked", "message": "Which regions?"})

    with later(days=4):
        assert requests.fire(1, "asked_ran_out") is None
    with later(days=5, minutes=1):
        assert requests.fire(1, "asked_ran_out").to_state == "dropped"


def test_nobody_owed_an_answer_is_closed_for_silence(requests, box):
    thread = asked(requests, box)
    they_wrote(thread)

    with later(days=30):
        assert requests.fire(1, "asked_ran_out") is None


def test_somebody_suppressed_is_owed_none_and_leaves_by_time(requests, box):
    thread = asked(requests, box)
    they_wrote(thread, body="stop")
    suppress_email(ASKER, reason="asked to stop")

    assert requests.fire(1, "asked_turn") is None
    with later(days=6):
        assert requests.fire(1, "asked_ran_out").to_state == "dropped"


# ── The thread finds its entity ──


def test_a_merge_keeps_the_entity_the_thread_belongs_to(box):
    from openoutsend.emails import threads

    stray = maillog.inbound(box, thread=Thread.objects.create(mailbox=box), message_id="first@acme.com")
    ours = Thread.objects.create(mailbox=box, machine="request", entity="1")
    question = maillog.outbound(box, thread=ours, message_id="question@corp.com")
    question.in_reply_to = "first@acme.com"
    question.save()

    kept = threads.assign(question)

    assert kept.pk == stray.thread_id
    assert (kept.machine, kept.entity) == ("request", "1")


# ── Declaring ──


def test_a_timed_row_needs_a_wait_and_a_turn_row_a_conversation(table):
    def options(rows):
        return dict(transitions=rows, load=table.__getitem__, address=lambda request: request.email)

    class Plain(States):
        new = InitialState("New")
        open = State("Open")
        done = FinalState("Done")

    def go(entity):
        return Plain.open

    with pytest.raises(MachineError, match="timed row but is not a Wait"):
        MailMachine(Plain, **options([(Plain.new, Plain.open, go), (Plain.open, Plain.done, timed)]))
    with pytest.raises(MachineError, match="turn row but is not a Conversation"):
        MailMachine(Plain, **options([(Plain.new, Plain.open, go), (Plain.open, Plain.done, turn)]))


def test_a_wait_needs_its_timed_row_and_a_conversation_a_way_to_stay(table):
    class Talk(States):
        new = InitialState("New")
        held = Wait("Held a day", rests=timedelta(days=1))
        talking = Conversation("Talking", prompt="Answer them.", rests=timedelta(days=5))
        done = FinalState("Done")

    def hold(entity):
        return Talk.held

    def skip(entity):
        return Talk.talking

    def options(rows):
        return dict(transitions=rows, load=table.__getitem__, address=lambda entity: entity.email)

    stay = [(Talk.talking, Talk.talking, turn), (Talk.talking, Talk.done, turn)]
    ends = [(Talk.talking, Talk.done, timed)]
    start = [(Talk.new, Talk.held, hold)]

    with pytest.raises(MachineError, match="held is a Wait with no timed row"):
        MailMachine(Talk, **options([*start, (Talk.held, Talk.talking, skip), *stay, *ends]))
    with pytest.raises(MachineError, match="talking is a Conversation with no turn row back to itself"):
        MailMachine(Talk, **options(
            [*start, (Talk.held, Talk.talking, timed), (Talk.talking, Talk.done, turn), *ends]))
    with pytest.raises(MachineError, match="two timed rows"):
        MailMachine(Talk, **options(
            [*start, (Talk.held, Talk.talking, timed), (Talk.held, Talk.done, timed), *stay, *ends]))


@pytest.mark.parametrize("rests", [5, "a week", None])
def test_a_wait_needs_a_length(rests):
    with pytest.raises(TypeError, match="rests= must be"):
        Wait("Resting", rests=rests)


def test_a_conversation_needs_a_prompt():
    with pytest.raises(TypeError, match="prompt= must be"):
        Conversation("Talking", prompt="  ", rests=timedelta(days=1))
