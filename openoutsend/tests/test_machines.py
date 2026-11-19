"""A wait and a conversation over a thread, on machines that have no deal in them.

The entity is a request somebody sent in: a plain object with an id and an address,
kept in a dict. It is asked a question by email, rests in a conversation, and is moved
by the answer, by a quiet week, or by a session standing in for the model. A second
machine waits for a visitor to pay, and follows up while they don't.
"""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from agentic_fsm import FinalState, InitialState, Machine, MachineError, State, States, TransitionError, check
from django.utils import timezone
from pydantic_ai.models.test import TestModel

from openoutsend.emails.models import Message, Thread
from openoutsend.errors import OutsendError
from openoutsend.leads.suppression import suppress_email
from openoutsend.machines import Conversation, FollowUp, MailMachine, Turn, Wait
from openoutsend.machines.machine import running
from openoutsend.tests.emails import maillog

pytestmark = pytest.mark.django_db

ASKER = "ann@acme.com"
HOW_TO_ANSWER = "They were asked which country they sell in. Accept once they name one."


def hold(request):
    return Request.held


def ask(request):
    running().send(request, "One question", "Which country do you sell in?", mailbox=request.box)
    return Request.asked


class Request(States):
    received = InitialState("Just arrived", step=hold)
    held = State("Held a day before anything is asked", step=Wait(rests=timedelta(days=1)))
    asking = State("Due its question", step=ask)
    asked = State("Asked a question, talking it over",
                  step=Conversation(prompt=HOW_TO_ANSWER, rests=timedelta(days=5), to="dropped"))
    accepted = FinalState("Their answer makes the request workable")
    dropped = FinalState("They declined, or never answered")


# fmt: off
REQUEST_EDGES = [
    (Request.received, Request.held),
    (Request.held,     Request.asking),
    (Request.asking,   Request.asked),
    (Request.asked,    Request.asked),
    (Request.asked,    Request.accepted),
    (Request.asked,    Request.dropped),
]
# fmt: on


@pytest.fixture
def box(operator):
    return maillog.mailbox("desk@corp.com")


@pytest.fixture
def table(box):
    return {"1": SimpleNamespace(pk=1, email=ASKER, box=box, paid=False)}


def mail_machine(states, edges, table, **options):
    return MailMachine(states, edges, load=table.__getitem__, address=lambda entity: entity.email, **options)


@pytest.fixture
def requests(table):
    return mail_machine(Request, REQUEST_EDGES, table, name="request")


@pytest.fixture(autouse=True)
def nothing_leaves_the_machine():
    with patch("openoutsend.emails.sender._deliver") as deliver:
        yield deliver


def later(**gap):
    """The clock a wait is measured against, set ahead by *gap*."""
    return patch("openoutsend.machines.machine.now", return_value=timezone.now() + timedelta(**gap))


def backdate(machine, **gap):
    """Move the entity's newest log row back by *gap*: it has rested that long already."""
    from agentic_fsm.backends.django.models import LogEntry

    newest = LogEntry.objects.filter(machine=machine.name, entity="1").order_by("pk").last()
    newest.at -= timedelta(**gap)
    newest.save()


def asked(requests) -> Thread:
    """Request 1 resting in `asked`, its question sent; returns the thread."""
    requests.start(1)
    requests.fire(1)
    with later(days=1):
        requests.fire(1)
    requests.fire(1)
    return Thread.objects.get(machine="request", entity="1")


def they_wrote(thread, body="Italy, mostly.", **options) -> Message:
    return maillog.inbound(thread.mailbox, thread=thread, sender=ASKER, body=body, **options)


def model_saying(**fields):
    return patch("openoutsend.core.llm.get_llm_model", return_value=TestModel(custom_output_args=fields))


# ── A wait ──


def test_a_wait_says_not_now_until_it_runs_out(requests):
    requests.start(1)
    requests.fire(1)

    with later(hours=23):
        assert requests.fire(1) is None
        assert requests.due() == []
        assert requests.progress_all() == []
    with later(hours=25):
        assert requests.due() == ["1"]
        (move,) = requests.progress_all()

    assert (move.from_state, move.to_state, move.decided_by) == ("held", "asking", "code")


def test_a_wait_counts_from_the_move_that_brought_the_entity_there(requests):
    requests.start(1)
    backdate(requests, days=3)
    requests.fire(1)

    assert requests.entered_at(1) == requests.history(1)[-1].at
    assert requests.fire(1) is None


def test_a_wait_may_say_when_it_runs_out_from_the_entity(table):
    class Parcel(States):
        packed = InitialState("Packed", step=lambda parcel: Parcel.stored)
        stored = State("Stored until its pickup date",
                       step=Wait(rests=lambda parcel, entered_at: parcel.pickup_at))
        gone = FinalState("Picked up")

    parcels = mail_machine(Parcel, [(Parcel.packed, Parcel.stored), (Parcel.stored, Parcel.gone)], table)
    table["1"].pickup_at = timezone.now() + timedelta(days=2)
    parcels.start(1)
    parcels.fire(1)

    with later(days=1):
        assert parcels.fire(1) is None
    with later(days=2, minutes=1):
        assert parcels.fire(1).to_state == "gone"


def test_a_wait_runs_only_in_a_mail_machine(table):
    class Parcel(States):
        stored = InitialState("Stored a day", step=Wait(rests=timedelta(days=1)))
        gone = FinalState("Picked up")

    parcels = Machine(Parcel, [(Parcel.stored, Parcel.gone)], load=table.__getitem__,
                      backend="agentic_fsm.backends.locmem")
    parcels.start(1)

    with pytest.raises(OutsendError, match="only as a step of a MailMachine"):
        parcels.fire(1)


# ── Asking by email ──


def test_a_step_opens_a_thread_kept_against_the_entity(requests, nothing_leaves_the_machine):
    thread = asked(requests)

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


def test_with_nothing_incoming_the_conversation_says_not_now(requests):
    asked(requests)

    assert requests.incoming(requests.load("1")) is None
    assert requests.fire(1) is None
    assert requests.due() == []


def test_a_bounce_or_an_out_of_office_is_not_incoming(requests):
    thread = asked(requests)
    they_wrote(thread, kind="auto_reply")
    they_wrote(thread, kind="bounce")

    assert requests.fire(1) is None


def test_the_model_answers_and_the_turn_is_a_move_from_the_state_to_itself(requests):
    thread = asked(requests)
    reply = they_wrote(thread)

    assert requests.due() == ["1"]
    with model_saying(to="asked", message="Thanks. Which regions of Italy?"):
        moves = requests.progress(1)

    (move,) = moves
    assert (move.from_state, move.to_state) == ("asked", "asked")
    assert move.decided_by == "llm:test:test"
    assert move.decision == {"to": "asked", "message": "Thanks. Which regions of Italy?"}
    answer = thread.messages.order_by("pk").last()
    assert answer.is_outbound and answer.body_text == "Thanks. Which regions of Italy?"
    assert (answer.to_address, answer.subject) == (ASKER, "Re: One question")
    assert answer.in_reply_to == reply.message_id
    assert answer.prompt == HOW_TO_ANSWER
    assert requests.incoming(requests.load("1")) is None


def test_the_model_reads_the_whole_thread_and_the_conversations_prompt(requests):
    thread = asked(requests)
    they_wrote(thread, body="Italy, mostly.")

    brief = requests.brief(1)

    assert "Which country do you sell in?" in brief.prompt
    assert "THEY WROTE:\nItaly, mostly." in brief.prompt
    assert brief.prompt.endswith(HOW_TO_ANSWER)
    assert "set `to` to `asked`" in brief.instructions[0]
    assert [value for value, _ in brief.choices] == ["asked", "accepted", "dropped"]


def test_the_model_leaves_by_one_of_the_states_edges_and_sends_nothing(requests):
    thread = asked(requests)
    they_wrote(thread)

    with model_saying(to="accepted"):
        (move,) = requests.progress(1)

    assert move.to_state == "accepted"
    assert thread.messages.filter(direction="out").count() == 1


def test_a_session_takes_the_turn_and_its_message_is_sent(requests):
    thread = asked(requests)
    they_wrote(thread)

    move = requests.fire(1, answer={"to": "asked", "message": "Which regions?"})

    assert (move.to_state, move.decided_by) == ("asked", "agent")
    assert thread.messages.order_by("pk").last().body_text == "Which regions?"


def test_a_session_has_no_turn_to_take_while_nothing_is_incoming(requests):
    asked(requests)

    with pytest.raises(TransitionError, match="not now"):
        requests.fire(1, answer={"to": "accepted"})

    assert requests.state_of(1) is Request.asked


def test_staying_with_nothing_to_send_is_refused(requests):
    thread = asked(requests)
    they_wrote(thread)

    with pytest.raises(TransitionError, match="message is required"):
        requests.fire(1, answer={"to": "asked"})

    assert len(requests.history(1)) == 4


def test_a_send_that_raises_writes_no_move(requests, nothing_leaves_the_machine):
    thread = asked(requests)
    they_wrote(thread)
    nothing_leaves_the_machine.side_effect = OSError("connection reset")

    with pytest.raises(Exception, match="connection reset"):
        requests.fire(1, answer={"to": "asked", "message": "Which regions?"})

    assert requests.state_of(1) is Request.asked
    assert len(requests.history(1)) == 4


# ── A conversation that goes quiet ──


def test_a_quiet_conversation_leaves_by_time_counted_from_its_last_turn(requests):
    thread = asked(requests)
    backdate(requests, days=4)
    they_wrote(thread)
    requests.fire(1, answer={"to": "asked", "message": "Which regions?"})

    with later(days=4):
        assert requests.fire(1) is None
    with later(days=5, minutes=1):
        move = requests.fire(1)

    assert (move.to_state, move.decided_by) == ("dropped", "code")


def test_nobody_owed_an_answer_is_closed_for_silence(requests):
    thread = asked(requests)
    they_wrote(thread)

    with later(days=30), model_saying(to="asked", message="Sorry for the wait. Which regions?"):
        assert requests.fire(1).to_state == "asked"


def test_somebody_suppressed_is_owed_none_and_leaves_by_time(requests):
    thread = asked(requests)
    they_wrote(thread, body="stop")
    suppress_email(ASKER, reason="asked to stop")

    assert requests.fire(1) is None
    with later(days=6):
        assert requests.fire(1).to_state == "dropped"


# ── A conversation that waits for an action and follows up ──


nudging = Conversation(
    prompt="They were sent a quote. Answer what they ask.",
    follow_ups=[FollowUp(timedelta(days=1), "Did the quote reach you?"), timedelta(days=4)],
    rests=timedelta(days=5),
    to="quiet",
)


def quote(visitor):
    running().send(visitor, "Your quote", "It is attached.", mailbox=visitor.box)
    return Visitor.quoted


@check
def quoted(visitor):
    if visitor.paid:
        return Visitor.ordered
    return nudging


class Visitor(States):
    new = InitialState("Asked for a quote", step=quote)
    quoted = State("Sent a quote; we wait for the payment", step=quoted)
    ordered = FinalState("Paid; set only when a payment arrived")
    handed = FinalState("Passed to a person")
    quiet = FinalState("Never paid, never answered")


# fmt: off
VISITOR_EDGES = [
    (Visitor.new,    Visitor.quoted),
    (Visitor.quoted, Visitor.quoted),
    (Visitor.quoted, Visitor.ordered),
    (Visitor.quoted, Visitor.handed),
    (Visitor.quoted, Visitor.quiet),
]
# fmt: on


@pytest.fixture
def visitors(table):
    machine = mail_machine(Visitor, VISITOR_EDGES, table, name="visitor")
    machine.start(1)
    machine.fire(1)
    return machine


def followed_up_once(visitors):
    backdate(visitors, days=1, minutes=1)
    return visitors.fire(1)


def test_a_fact_moves_the_entity_before_the_conversation_is_asked(visitors, table):
    table["1"].paid = True

    move = visitors.fire(1)

    assert (move.to_state, move.decided_by, move.decision) == ("ordered", "code", None)


def test_a_written_follow_up_is_sent_when_due_and_logged_as_one_move(visitors):
    with later(hours=23):
        assert visitors.fire(1) is None
        assert visitors.due() == []
    with later(days=1, minutes=1):
        assert visitors.due() == ["1"]
        (move,) = visitors.progress(1)

    assert (move.from_state, move.to_state, move.decided_by) == ("quoted", "quoted", "code")
    assert move.decision == {"to": "quoted", "message": "Did the quote reach you?", "follow_up": 1}
    opener, sent = Thread.objects.get(machine="visitor", entity="1").messages.order_by("pk")
    assert (sent.to_address, sent.subject, sent.body_text) == (ASKER, "Re: Your quote", "Did the quote reach you?")
    assert sent.in_reply_to == opener.message_id


def test_the_next_follow_up_counts_from_the_last_and_the_model_writes_it(visitors):
    followed_up_once(visitors)

    with later(days=3):
        assert visitors.fire(1) is None
    with later(days=4, minutes=1), model_saying(to="quoted", message="Shall I hold the price?"):
        move = visitors.fire(1)

    assert (move.to_state, move.decided_by) == ("quoted", "llm:test:test")
    assert move.decision == {"to": "quoted", "message": "Shall I hold the price?", "follow_up": 2}
    assert Message.objects.filter(direction="out").order_by("pk").last().body_text == "Shall I hold the price?"


def test_a_session_writes_the_models_follow_up_and_is_offered_only_the_state(visitors):
    followed_up_once(visitors)

    with later(days=4, minutes=1):
        brief = visitors.brief(1)
        move = visitors.fire(1, answer={"to": "quoted", "message": "Shall I hold the price?"})

    assert [value for value, _ in brief.choices] == ["quoted"]
    assert "Write follow-up 2 of 2." in brief.prompt
    assert (move.decided_by, move.decision["follow_up"]) == ("agent", 2)


def test_a_reply_is_answered_and_delays_the_next_follow_up_without_using_one_up(visitors):
    thread = Thread.objects.get(machine="visitor", entity="1")
    backdate(visitors, days=2)
    they_wrote(thread, body="Is delivery included?")

    answer = visitors.fire(1, answer={"to": "quoted", "message": "It is."})
    with later(hours=23):
        assert visitors.fire(1) is None
    with later(days=1, minutes=1):
        follow_up = visitors.fire(1)

    assert answer.decision == {"to": "quoted", "message": "It is."}
    assert follow_up.decision["follow_up"] == 1


def test_the_model_may_hand_the_entity_on_when_it_answers(visitors):
    they_wrote(Thread.objects.get(machine="visitor", entity="1"), body="Can we talk?")

    with model_saying(to="handed"):
        move = visitors.fire(1)

    assert move.to_state == "handed"


def test_silence_after_the_last_follow_up_closes_the_conversation(visitors):
    followed_up_once(visitors)
    backdate(visitors, days=4, minutes=1)
    visitors.fire(1, answer={"to": "quoted", "message": "Shall I hold the price?"})

    with later(days=4):
        assert visitors.fire(1) is None
    with later(days=5, minutes=1):
        move = visitors.fire(1)

    assert (move.to_state, move.decided_by) == ("quiet", "code")
    assert [m.to_state for m in visitors.history(1)] == ["new", "quoted", "quoted", "quoted", "quiet"]


def test_a_fact_still_moves_the_entity_between_follow_ups(visitors, table):
    followed_up_once(visitors)
    table["1"].paid = True

    assert visitors.fire(1).to_state == "ordered"


def test_nobody_suppressed_is_followed_up(visitors):
    suppress_email(ASKER, reason="asked to stop")

    with later(days=2):
        assert visitors.fire(1) is None
    with later(days=5, minutes=1):
        assert visitors.fire(1).to_state == "quiet"

    assert Message.objects.filter(direction="out").count() == 1


def test_a_follow_up_needs_a_thread_to_go_in(table):
    class Guest(States):
        waiting = InitialState("Waiting", step=Conversation(
            prompt="Answer them.", follow_ups=[FollowUp(timedelta(days=1), "Hello?")], rests=timedelta(days=5)))
        gone = FinalState("Gone")

    guests = mail_machine(Guest, [(Guest.waiting, Guest.waiting), (Guest.waiting, Guest.gone)], table)
    guests.start(1)

    with later(days=2), pytest.raises(OutsendError, match="no thread to follow up in"):
        guests.fire(1)

    assert len(guests.history(1)) == 1


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


def talk(table, edges=(("talking", "talking"), ("talking", "done"), ("talking", "dropped")), **conversation):
    """A machine whose one state holds a conversation, with *edges* given by name."""

    class Talk(States):
        talking = InitialState("Talking", step=Conversation(
            prompt="Answer them.", rests=timedelta(days=5), **conversation))
        done = FinalState("Done")
        dropped = FinalState("Dropped")

    return mail_machine(Talk, [(Talk[source], Talk[target]) for source, target in edges], table)


def test_a_conversation_needs_an_edge_back_to_itself(table):
    with pytest.raises(MachineError, match="talking holds a Conversation but has no edge back to itself"):
        talk(table, edges=[("talking", "done"), ("talking", "dropped")], to="dropped")


def test_a_wait_with_several_ways_out_says_which(table):
    with pytest.raises(MachineError, match="say where its wait runs out with to="):
        talk(table)
    with pytest.raises(MachineError, match="no edge to 'gone'"):
        talk(table, to="gone")

    assert talk(table, to="dropped").states.talking.step.to == "dropped"


@pytest.mark.parametrize("rests", [5, "a week", None])
def test_a_wait_needs_a_length(rests):
    with pytest.raises(TypeError, match="rests= must be"):
        Wait(rests=rests)


def test_a_conversation_needs_a_prompt():
    with pytest.raises(TypeError, match="prompt= must be"):
        Conversation(prompt="  ", rests=timedelta(days=1))


def test_what_a_wait_or_a_conversation_runs_must_be_a_function():
    with pytest.raises(TypeError, match="then= must be a function"):
        Wait(rests=timedelta(days=1), then="close it")
    with pytest.raises(TypeError, match="effect= must be a function"):
        Conversation(prompt="Answer them.", rests=timedelta(days=1), effect="record it")
    with pytest.raises(TypeError, match="schema= must be a subclass of Turn"):
        Conversation(prompt="Answer them.", rests=timedelta(days=1), schema=dict)


def test_a_follow_up_needs_a_gap_and_real_text():
    with pytest.raises(TypeError, match="after= must be"):
        FollowUp("tomorrow")
    with pytest.raises(TypeError, match="text= must be"):
        FollowUp(timedelta(days=1), "  ")


# ── A wait that records having waited in vain ──


def shelving(table, **wait):
    """A machine whose one wait is its initial state: a parcel shelved a day, then gone."""

    class Parcel(States):
        shelved = InitialState("Shelved a day", step=Wait(rests=timedelta(days=1), **wait))
        gone = FinalState("Gone")

    return mail_machine(Parcel, [(Parcel.shelved, Parcel.gone)], table, name="parcel")


def test_a_wait_may_be_the_initial_states_step(table):
    parcels = shelving(table)

    move = parcels.start(1)

    assert (move.to_state, parcels.state_of(1)) == ("shelved", parcels.states.shelved)
    with later(hours=23):
        assert parcels.fire(1) is None
    with later(hours=25):
        assert parcels.fire(1).to_state == "gone"


def test_then_runs_with_the_move_when_the_wait_runs_out_and_not_before(table):
    forgotten = []
    parcels = shelving(table, then=forgotten.append)
    parcels.start(1)

    with later(hours=23):
        assert parcels.fire(1) is None
        assert parcels.due() == []
        assert parcels.progress_all() == []
    assert forgotten == []

    with later(hours=25):
        assert parcels.due() == ["1"]
        assert forgotten == []
        move = parcels.fire(1)

    assert forgotten == [table["1"]]
    assert (move.to_state, move.decided_by) == ("gone", "code")


def test_a_then_that_raises_writes_no_move(table):
    def fail(parcel):
        raise OSError("disk full")

    parcels = shelving(table, then=fail)
    parcels.start(1)

    with later(hours=25), pytest.raises(OSError, match="disk full"):
        parcels.fire(1)

    assert parcels.state_of(1) is parcels.states.shelved
    assert len(parcels.history(1)) == 1


# ── A conversation whose turn says more, and does more ──


class Verdict(Turn):
    country: str


def talking(table, **conversation):
    """A machine whose one conversation opens with a question; returns it."""

    def ask(request):
        running().send(request, "One question", "Which country do you sell in?", mailbox=request.box)
        return Talk.talking

    class Talk(States):
        new = InitialState("New", step=ask)
        talking = State("Talking it over", step=Conversation(
            prompt=HOW_TO_ANSWER, rests=timedelta(days=5), to="dropped", **conversation))
        accepted = FinalState("Their answer makes the request workable")
        dropped = FinalState("They declined, or never answered")

    # fmt: off
    edges = [
        (Talk.new,     Talk.talking),
        (Talk.talking, Talk.talking),
        (Talk.talking, Talk.accepted),
        (Talk.talking, Talk.dropped),
    ]
    # fmt: on
    machine = mail_machine(Talk, edges, table, name="talk")
    machine.start(1)
    machine.fire(1)
    return machine


def test_a_turns_own_field_is_logged_with_its_move(table):
    talks = talking(table, schema=Verdict)
    they_wrote(Thread.objects.get(machine="talk", entity="1"))

    with model_saying(to="accepted", country="Italy"):
        (move,) = talks.progress(1)

    assert move.to_state == "accepted"
    assert move.decision == {"to": "accepted", "message": None, "country": "Italy"}


def test_a_turns_own_field_is_validated(table):
    talks = talking(table, schema=Verdict)
    they_wrote(Thread.objects.get(machine="talk", entity="1"))

    with pytest.raises(TransitionError, match="country"):
        talks.fire(1, answer={"to": "accepted"})
    with pytest.raises(TransitionError, match="message is required"):
        talks.fire(1, answer={"to": "talking", "country": "Italy"})

    assert talks.state_of(1) is talks.states.talking
    assert "country" in talks.brief(1).schema["required"]


def test_a_sessions_turn_carries_the_field_too(table):
    talks = talking(table, schema=Verdict)
    they_wrote(Thread.objects.get(machine="talk", entity="1"))

    move = talks.fire(1, answer={"to": "talking", "message": "Only Italy?", "country": "Italy"})

    assert move.decision == {"to": "talking", "message": "Only Italy?", "country": "Italy"}
    assert move.decided_by == "agent"


def test_the_effect_runs_for_a_turn_that_stays_after_its_message_is_sent(table):
    seen = []

    def record(request, decision):
        sent = Message.objects.filter(direction="out").order_by("pk").last()
        seen.append((request, decision.to.value, decision.message, sent.body_text))

    talks = talking(table, effect=record)
    they_wrote(Thread.objects.get(machine="talk", entity="1"))

    with model_saying(to="talking", message="Which regions?"):
        (move,) = talks.progress(1)

    assert seen == [(table["1"], "talking", "Which regions?", "Which regions?")]
    assert (move.from_state, move.to_state) == ("talking", "talking")


def test_the_effect_runs_for_a_turn_that_leaves_and_nothing_is_sent(table):
    seen = []
    talks = talking(table, schema=Verdict,
                    effect=lambda request, decision: seen.append((request, decision.to.value, decision.country)))
    thread = Thread.objects.get(machine="talk", entity="1")
    they_wrote(thread)

    move = talks.fire(1, answer={"to": "accepted", "country": "Italy"})

    assert seen == [(table["1"], "accepted", "Italy")]
    assert move.to_state == "accepted"
    assert thread.messages.filter(direction="out").count() == 1


def test_the_effect_runs_for_a_follow_up_after_it_is_sent(table):
    seen = []
    talks = talking(table, follow_ups=[FollowUp(timedelta(days=1), "Still there?")],
                    effect=lambda request, decision: seen.append((decision.follow_up, decision.message)))

    with later(days=1, minutes=1):
        talks.fire(1)

    assert seen == [(1, "Still there?")]


def test_an_effect_that_raises_writes_no_move(table):
    def fail(request, decision):
        raise OSError("disk full")

    talks = talking(table, effect=fail)
    they_wrote(Thread.objects.get(machine="talk", entity="1"))

    with pytest.raises(OSError, match="disk full"):
        talks.fire(1, answer={"to": "accepted"})

    assert talks.state_of(1) is talks.states.talking
    assert len(talks.history(1)) == 2


def test_no_effect_runs_while_nothing_is_incoming(table):
    seen = []
    talks = talking(table, effect=lambda request, decision: seen.append(decision))

    assert talks.fire(1) is None
    assert seen == []
