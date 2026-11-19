"""The deal machine: the machine a sequence folder becomes, its waits, its sends and its ends.

The folder here has three email steps, so no model is called. Days are fixed dates of
November 2026 (the 13th is a Friday) read on a UTC clock with the weekend pause on, so a
gap in business days can be told from one in calendar days.
"""
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from agentic_fsm import TransitionError

from openoutsend.core import sequences
from openoutsend.core.sequences import SequenceError, key
from openoutsend.emails.models import Message, Thread
from openoutsend.emails.steps.send import Plan
from openoutsend.leads import lifecycle
from openoutsend.leads.lifecycle import machine_for, machine_of
from openoutsend.leads.models import Deal, Outcome, Suppression
from openoutsend.leads.states import COMPLETED, CONVERSING, awaiting, machine_name, sending
from openoutsend.planner import due_deals
from openoutsend.tests.emails import maillog
from openoutsend.tests.factories import DealFactory, LeadFactory, put_in_state

pytestmark = pytest.mark.django_db

STEPS = {
    "E0-2d-hello.email.j2": "Subject: Your ten leads\n\nHi {{ first_name }}, they are ready.",
    "E1-3d-nudge.email.j2": "Did they fit, {{ first_name }}?",
    "E2-5d-last.email.j2": "Last note, {{ first_name }}.",
}

OPEN_STATES = [awaiting(0), sending(0), awaiting(1), sending(1), awaiting(2), sending(2), CONVERSING]


def _folder(tmp_path, name: str, steps: dict = STEPS) -> str:
    """A sequence folder named *name* holding *steps*, under the key `--sequence` gives it."""
    folder = tmp_path / name
    folder.mkdir()
    for filename, body in steps.items():
        (folder / filename).write_text(body, encoding="utf-8")
    return key(str(folder))


@pytest.fixture
def relaunch(tmp_path):
    """The three-step folder's name."""
    return _folder(tmp_path, "relaunch")


@pytest.fixture
def machine(relaunch):
    return machine_for(sequences.named(relaunch))


@pytest.fixture(autouse=True)
def a_utc_clock_with_weekends_off_work(settings):
    settings.OUTSEND_OPERATOR_TIMEZONE = "UTC"
    settings.OUTSEND_ENFORCE_WEEKEND_PAUSE = True


@pytest.fixture(autouse=True)
def nothing_leaves_the_machine():
    with patch("openoutsend.emails.sender._deliver") as deliver:
        yield deliver


@pytest.fixture
def box(operator):
    return maillog.mailbox("ada@corp.com")


def november(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 11, day, hour, tzinfo=UTC)


def on(moment: datetime):
    """The clock a wait is measured against, set to *moment*."""
    return patch("openoutsend.machines.machine.now", return_value=moment)


def _deal(sequence: str, **options) -> Deal:
    options.setdefault("lead", LeadFactory(email="asha@example.com", first_name="Asha"))
    return DealFactory(sequence=sequence, **options)


def _written_to(deal, box, state: str) -> Deal:
    """*deal* with its first email sent from *box*, then placed in *state*."""
    put_in_state(deal, sending(0))
    deal.plan = Plan(box)
    machine_of(deal).fire(deal.pk, entity=deal)
    put_in_state(deal, state)
    return deal


def no_box_is_free():
    return patch("openoutsend.planner.free_for_first_email", return_value=None)


# ── The graph ──


def test_a_folder_becomes_two_states_a_step_then_the_conversation_and_the_end(machine, relaunch):
    graph = machine.graph()

    assert graph.name == f"deal-{relaunch}" == machine_name(relaunch)
    assert [state.value for state in graph.states] == [
        "awaiting_E0", "sending_E0", "awaiting_E1", "sending_E1", "awaiting_E2", "sending_E2",
        "conversing", "completed",
    ]
    assert machine.initial.value == "awaiting_E0"
    assert [state.kind for state in graph.states].count("final") == 1


def test_every_state_has_its_one_handler_and_its_edges(machine):
    graph = machine.graph()

    assert [state.handler for state in graph.states] == [
        "Awaiting", "Sending", "Awaiting", "Sending", "Awaiting", "Sending", "Conversing", None,
    ]
    assert sorted(graph.edges) == sorted([
        ("awaiting_E0", "sending_E0"), ("awaiting_E0", "awaiting_E1"), ("awaiting_E0", "awaiting_E2"),
        ("sending_E0", "awaiting_E1"),
        ("awaiting_E1", "sending_E1"), ("sending_E1", "awaiting_E2"),
        ("awaiting_E2", "sending_E2"), ("sending_E2", "conversing"),
        ("conversing", "conversing"),
        *[(state, "completed") for state in OPEN_STATES],
    ])


def test_every_open_state_can_be_ended_and_nothing_leaves_completed(machine):
    edges = machine.graph().edges

    assert sorted(source for source, target in edges if target == "completed") == sorted(OPEN_STATES)
    assert not [edge for edge in edges if edge[0] == "completed"]


def test_a_one_step_folder_has_no_later_step_to_open_with(tmp_path):
    single = machine_for(sequences.named(_folder(tmp_path, "single", {"E0-a.prompt.j2": "Write."})))

    graph = single.graph()

    assert [state.value for state in graph.states] == ["awaiting_E0", "sending_E0", "conversing", "completed"]
    assert ("sending_E0", "conversing") in graph.edges
    assert [target for source, target in graph.edges if source == "awaiting_E0"] == ["sending_E0", "completed"]


# ── A folder with no conversation ──


@pytest.fixture
def broadcast(monkeypatch, relaunch):
    """The three-step folder's machine in an install that holds no conversations."""
    monkeypatch.setenv("OUTSEND_CONVERSATIONS", "off")
    return machine_for(sequences.named(relaunch))


def test_conversations_are_off_unless_the_install_turns_them_on(monkeypatch, machine, relaunch):
    monkeypatch.delenv("OUTSEND_CONVERSATIONS")

    unset = machine_for(sequences.named(relaunch))

    assert unset is not machine
    assert "conversing" not in [state.value for state in unset.graph().states]
    assert "conversing" in [state.value for state in machine.graph().states]


def test_a_folder_with_no_conversation_ends_at_its_last_send(broadcast):
    graph = broadcast.graph()

    assert [state.value for state in graph.states] == [
        "awaiting_E0", "sending_E0", "awaiting_E1", "sending_E1", "awaiting_E2", "sending_E2", "completed",
    ]
    assert [target for source, target in graph.edges if source == "sending_E2"] == ["completed"]


def test_the_last_send_of_a_broadcast_ends_the_deal_sent(broadcast, relaunch, box):
    deal = _written_to(_deal(relaunch), box, sending(2))
    deal.plan = Plan(box)

    move = broadcast.fire(deal.pk, entity=deal)

    deal.refresh_from_db()
    assert (move.from_state, move.to_state) == ("sending_E2", "completed")
    assert (deal.state, deal.outcome) == (COMPLETED, Outcome.SENT)
    assert deal.thread.messages.order_by("pk").last().body_text == "Last note, Asha."


def test_a_broadcast_deal_that_must_stop_is_ended_stopped_and_not_sent(broadcast, relaunch, box):
    deal = _written_to(_deal(relaunch), box, sending(2))
    deal.stop = True
    deal.plan = Plan(box)

    broadcast.fire(deal.pk, entity=deal)

    deal.refresh_from_db()
    assert (deal.state, deal.outcome) == (COMPLETED, Outcome.STOPPED)
    assert deal.thread.messages.count() == 1


# ── Reading the state ──


def test_a_deal_reads_its_state_from_its_folders_machine(machine, relaunch):
    deal = _deal(relaunch)

    assert deal.state == awaiting(0)
    assert machine.state_of(deal.pk) is machine.initial
    assert list(Deal.objects.in_state(awaiting(0))) == [deal]
    assert list(Deal.objects.open()) == [deal]
    assert not Deal.objects.in_state(sending(0), CONVERSING, COMPLETED).exists()


def test_a_deal_never_started_has_no_state():
    deal = Deal.objects.create(lead=LeadFactory())

    assert deal.state is None
    assert not Deal.objects.in_state(awaiting(0)).exists()
    assert not Deal.objects.open().exists()


# ── The wait ──


def test_the_first_step_waits_its_days_counted_from_start_at(machine, relaunch):
    """`E0-2d` from Friday the 13th is due as Tuesday the 17th begins: the weekend is not
    counted, and neither is when the deal was started in the machine (today)."""
    deal = _deal(relaunch, start_at=november(13), created_at=november(13))

    with on(november(16, 23)):
        assert machine.fire(deal.pk) is None
    with on(november(17, 1)):
        move = machine.fire(deal.pk)

    assert (move.from_state, move.to_state, move.decided_by) == ("awaiting_E0", "sending_E0", "code")
    assert deal.state == sending(0)


def test_a_later_step_waits_its_business_days_from_the_move_that_sent_the_one_before(
        machine, relaunch, box):
    """`E1-3d` after a send on Friday the 13th is due as Wednesday the 18th begins, wherever
    `start_at` is."""
    deal = _deal(relaunch, start_at=november(2), created_at=november(2),
                 mailbox=box, thread=Thread.objects.create(mailbox=box))
    put_in_state(deal, awaiting(1), at=november(13))

    assert machine.entered_at(deal.pk) == november(13)
    with on(november(17, 23)):
        assert machine.fire(deal.pk) is None
    with on(november(18, 1)):
        move = machine.fire(deal.pk)

    assert (move.from_state, move.to_state) == ("awaiting_E1", "sending_E1")


def test_a_wait_running_out_sends_nothing(machine, relaunch):
    deal = _deal(relaunch, start_at=november(13), created_at=november(13))

    with on(november(17, 1)):
        machine.fire(deal.pk)

    assert not Message.objects.exists()


# ── The send ──


def test_a_send_with_no_free_box_says_not_now(machine, relaunch):
    """A fire with no deal in hand asks the planner, and with no box free nothing goes."""
    deal = _deal(relaunch, state=sending(0))

    with no_box_is_free():
        assert machine.fire(deal.pk) is None

    assert deal.state == sending(0)
    assert not Message.objects.exists()


def test_a_send_with_a_planned_box_sends_and_moves_to_the_next_wait(machine, relaunch, box):
    deal = _deal(relaunch, state=sending(0))
    deal.plan = Plan(box)

    move = machine.fire(deal.pk, entity=deal)

    assert (move.from_state, move.to_state, move.decided_by) == ("sending_E0", "awaiting_E1", "code")
    (sent,) = Message.objects.all()
    assert (sent.to_address, sent.subject, sent.step, sent.sequence) == \
        ("asha@example.com", "Your ten leads", 0, relaunch)
    assert sent.body_text == "Hi Asha, they are ready."
    deal.refresh_from_db()
    assert (deal.state, deal.mailbox, deal.thread, deal.email_subject) == \
        (awaiting(1), box, sent.thread, "Your ten leads")
    assert (sent.thread.machine, sent.thread.entity) == (machine.name, str(deal.pk))
    assert machine.entered_at(deal.pk) == move.at


def test_a_send_fired_with_no_deal_in_hand_takes_the_box_the_planner_picks(machine, relaunch, box):
    deal = _deal(relaunch, state=sending(0))

    with patch("openoutsend.planner.free_for_first_email", return_value=box):
        move = machine.fire(deal.pk)

    assert move.to_state == awaiting(1)
    assert Message.objects.get().mailbox == box


def test_a_later_send_goes_in_the_deals_thread(machine, relaunch, box):
    deal = _written_to(_deal(relaunch), box, sending(1))
    deal.plan = Plan(box)

    move = machine.fire(deal.pk, entity=deal)

    assert (move.from_state, move.to_state) == ("sending_E1", "awaiting_E2")
    first, second = deal.thread.messages.order_by("pk")
    assert (second.step, second.subject, second.body_text) == (1, "Your ten leads", "Did they fit, Asha?")
    assert second.in_reply_to == first.message_id


def test_the_last_send_moves_the_deal_to_its_conversation(machine, relaunch, box):
    deal = _written_to(_deal(relaunch), box, sending(2))
    deal.plan = Plan(box)

    move = machine.fire(deal.pk, entity=deal)

    assert (move.from_state, move.to_state) == ("sending_E2", "conversing")
    assert (deal.state, Deal.objects.get(pk=deal.pk).outcome) == (CONVERSING, "")
    assert deal.thread.messages.order_by("pk").last().body_text == "Last note, Asha."


def test_a_deal_still_waiting_is_not_sent_even_with_a_box_in_hand(machine, relaunch, box):
    deal = _deal(relaunch, start_at=november(13), created_at=november(13))
    deal.plan = Plan(box)

    with on(november(13)):
        assert machine.fire(deal.pk, entity=deal) is None

    assert not Message.objects.exists()


# ── A deal that arrived late ──


def test_a_deal_that_arrived_with_every_step_past_its_grace_is_closed_missed(machine, relaunch):
    """Started on the 2nd, its steps were due on the 4th, the 9th and the 16th; it arrived
    on the 30th."""
    deal = _deal(relaunch, start_at=november(2), created_at=november(30))

    move = machine.fire(deal.pk)

    deal.refresh_from_db()
    assert (move.decided_by, move.from_state, move.to_state) == ("code", "awaiting_E0", "completed")
    assert (deal.state, deal.outcome) == (COMPLETED, Outcome.MISSED)
    assert not Message.objects.exists()


def test_a_deal_with_a_step_still_in_its_grace_is_not_closed(machine, relaunch):
    on_time = _deal(relaunch, start_at=november(13), created_at=november(13))
    late = _deal(relaunch, lead=LeadFactory(email="bo@example.com"),
                 start_at=november(2), created_at=november(13))

    with on(november(13)):
        assert machine.fire(on_time.pk) is None
        machine.fire(late.pk)

    assert (on_time.state, late.state) == (awaiting(0), awaiting(2))
    assert not Deal.objects.exclude(outcome="").exists()


def test_a_deal_whose_first_step_is_in_its_grace_does_not_open_later(machine, relaunch):
    """`E0` was due on Wednesday the 4th and the deal arrived on Friday the 6th, two
    business days late: it is sent its first step, which is due already."""
    deal = _deal(relaunch, start_at=november(2), created_at=november(6))

    with on(november(6)):
        move = machine.fire(deal.pk)

    assert (move.from_state, move.to_state) == ("awaiting_E0", "sending_E0")


def test_a_late_deal_skips_to_the_first_step_not_past_its_grace(machine, relaunch):
    """Started on Monday the 2nd: `E0` was due on the 4th and `E1` on the 9th. Arriving on
    Friday the 13th, both are past their grace and `E2`, due on Monday the 16th, is not."""
    deal = _deal(relaunch, start_at=november(2), created_at=november(13))

    move = machine.fire(deal.pk)

    assert (move.decided_by, move.from_state, move.to_state) == ("code", "awaiting_E0", "awaiting_E2")
    assert deal.state == awaiting(2)


def test_the_step_a_late_deal_opens_with_is_due_as_counted_from_when_the_skipped_ones_were(
        machine, relaunch):
    """`E2` is due on the 16th, five business days after `E1` was due, not five after the
    deal came to wait for it."""
    deal = _deal(relaunch, start_at=november(2), created_at=november(13))
    machine.fire(deal.pk)

    with on(november(15, 23)):
        assert machine.fire(deal.pk) is None
    with on(november(16, 1)):
        move = machine.fire(deal.pk)

    assert (move.from_state, move.to_state) == ("awaiting_E2", "sending_E2")


def test_progress_carries_a_late_deal_to_the_step_it_opens_with(machine, relaunch):
    """One state's handler at a time: the skip, then that step's wait, then a send with no box."""
    deal = _deal(relaunch, start_at=november(2), created_at=november(13))

    with on(november(16, 1)), no_box_is_free():
        moves = machine.progress(deal.pk)

    assert [move.to_state for move in moves] == ["awaiting_E2", "sending_E2"]
    assert deal.state == sending(2)


# ── Ending a suppressed or stopped deal ──


@pytest.mark.parametrize("state", OPEN_STATES)
def test_a_deal_whose_address_is_suppressed_is_ended_from_every_open_state(machine, relaunch, state):
    deal = _deal(relaunch, state=state)
    Suppression.objects.create(email="asha@example.com")

    move = machine.fire(deal.pk, entity=deal)

    deal.refresh_from_db()
    assert (move.decided_by, move.from_state, move.to_state) == ("code", state, "completed")
    assert (deal.state, deal.outcome) == (COMPLETED, Outcome.UNSUBSCRIBED)


@pytest.mark.parametrize("state", OPEN_STATES)
def test_a_deal_whose_line_says_stop_is_ended_stopped_from_every_open_state(machine, relaunch, state):
    deal = _deal(relaunch, state=state, stop=True)

    move = machine.fire(deal.pk)

    deal.refresh_from_db()
    assert (move.from_state, move.to_state) == (state, "completed")
    assert (deal.state, deal.outcome) == (COMPLETED, Outcome.STOPPED)


def test_an_outcome_set_before_the_end_is_kept(machine, relaunch):
    """Whoever suppressed the address says why: a dead mailbox is not an opt-out."""
    deal = _deal(relaunch, state=awaiting(1))
    Suppression.objects.create(email="asha@example.com")
    deal.outcome = Outcome.UNDELIVERABLE

    machine.fire(deal.pk, entity=deal)

    deal.refresh_from_db()
    assert (deal.state, deal.outcome) == (COMPLETED, Outcome.UNDELIVERABLE)


def test_a_suppressed_address_comes_before_the_lines_stop(machine, relaunch):
    deal = _deal(relaunch, stop=True)
    Suppression.objects.create(email="asha@example.com")

    machine.fire(deal.pk)

    deal.refresh_from_db()
    assert deal.outcome == Outcome.UNSUBSCRIBED


@pytest.mark.parametrize("state", OPEN_STATES)
def test_a_deal_neither_suppressed_nor_stopped_is_not_ended(machine, relaunch, state):
    """Its state's handler does its own work, which here finds nothing due and no box free."""
    deal = _deal(relaunch, state=state)

    with no_box_is_free():
        assert machine.fire(deal.pk) is None

    deal.refresh_from_db()
    assert (deal.state, deal.outcome) == (state, "")


def test_an_ended_deal_cannot_be_ended_again(machine, relaunch):
    deal = _deal(relaunch, state=COMPLETED, stop=True)

    with pytest.raises(TransitionError, match="a final state"):
        machine.fire(deal.pk)


# ── One machine per folder ──


def test_two_folders_are_two_machines_under_two_names(tmp_path, relaunch):
    event = _folder(tmp_path, "event", {"E0-a.prompt.j2": "Write."})

    one, other = machine_for(sequences.named(relaunch)), machine_for(sequences.named(event))

    assert one is not other
    assert (one.name, other.name) == ("deal-relaunch", "deal-event")
    assert len(one.graph().states) == 8 and len(other.graph().states) == 4


def test_a_deal_of_one_folder_is_not_seen_by_the_other(tmp_path, relaunch):
    event = _folder(tmp_path, "event", {"E0-a.prompt.j2": "Write."})
    one, other = machine_for(sequences.named(relaunch)), machine_for(sequences.named(event))
    deal = _deal(relaunch, stop=True)

    assert machine_of(deal) is one
    assert other.state_of(deal.pk) is None
    assert other.history(deal.pk) == [] and other.progress_all() == []
    with pytest.raises(TransitionError, match="hasn't started"):
        other.fire(deal.pk)
    assert deal.state == awaiting(0)


def test_a_lead_in_two_folders_has_a_state_in_each(tmp_path, relaunch):
    event = _folder(tmp_path, "event", {"E0-a.prompt.j2": "Write."})
    lead = LeadFactory(email="asha@example.com")
    in_relaunch, in_event = _deal(relaunch, lead=lead, state=sending(1)), _deal(event, lead=lead)

    assert (in_relaunch.state, in_event.state) == (sending(1), awaiting(0))
    assert list(Deal.objects.in_state(awaiting(0))) == [in_event]


def test_the_machine_is_built_once_per_folder_as_it_reads_now(tmp_path, relaunch):
    before = machine_for(sequences.named(relaunch))
    assert machine_for(sequences.named(relaunch)) is before

    (tmp_path / "relaunch" / "E3-1d-more.email.j2").write_text("One more.", encoding="utf-8")
    after = machine_for(sequences.named(relaunch))

    assert after is not before
    assert "sending_E3" in [state.value for state in after.graph().states]


# ── A sequence's machine by name, for `agfsm --machine` ──


def test_the_built_in_sequences_machine_is_an_attribute_of_the_module():
    cold = lifecycle.cold

    assert cold is machine_for(sequences.cold())
    assert cold.name == "deal-cold"
    assert machine_of(DealFactory()) is cold


def test_a_folder_this_run_named_is_an_attribute_too(machine, relaunch):
    assert getattr(lifecycle, relaunch) is machine


def test_an_unknown_name_is_no_attribute(tmp_path):
    with patch.object(sequences, "operator_dir", return_value=tmp_path), \
            pytest.raises(AttributeError, match="no folder of that name"):
        lifecycle.nope

    assert not hasattr(lifecycle, "__nope__")


# ── A folder edited under its deals ──


def test_a_deal_resting_in_a_state_its_folder_no_longer_has_is_named(relaunch):
    deal = _deal(relaunch)
    put_in_state(deal, sending(7))

    with pytest.raises(SequenceError) as error:
        due_deals()

    assert f"deal {deal.pk}" in str(error.value)
    assert "'sending_E7'" in str(error.value) and "no longer has" in str(error.value)
