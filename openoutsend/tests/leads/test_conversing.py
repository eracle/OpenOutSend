"""A deal's conversation: the turn the model takes, what ending records, and silence.

Every step of the deal's sequence has gone and it rests in `conversing`. The lead writes
back, and the model (or a session in its place) answers in the thread or ends the deal.
What every `Recheck` does (nothing incoming, a turn's validation, silence counted from
the last turn) is tested once, in ``machines/test_machines.py``; this is the deal's own.
Days are fixed dates of November 2026 (the 13th is a Friday) on a UTC clock with the
weekend pause on.
"""
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from agentic_fsm import TransitionError

from openoutsend.core import sequences
from openoutsend.core.agents.outreach import render_frame
from openoutsend.core.sequences import SequenceError
from openoutsend.emails.models import Thread
from openoutsend.leads import conversing
from openoutsend.leads.lifecycle import machine_of
from openoutsend.leads.models import Deal, Outcome
from openoutsend.leads.states import COMPLETED, CONVERSING, awaiting, machine_name
from openoutsend.leads.suppression import is_suppressed
from openoutsend.machines.handlers import ModelTurn
from openoutsend.tests.emails import maillog
from openoutsend.tests.factories import DealFactory, LeadFactory, model_saying, put_in_state, sequence_folder

pytestmark = pytest.mark.django_db

LEAD = "asha@example.com"
OPENER = "Which of the ten leads was wrong?"
STEP = {"E0-a.email.j2": "Subject: Your ten leads\n\nThey are ready."}
OWN_PROMPT = "Answer {{ first_name }} about {{ product }}. End it once they name a country."


def _folder(tmp_path, name: str, **files) -> str:
    """A one-step sequence folder named *name*, with *files* beside the step, as its key."""
    return sequence_folder(tmp_path, name, {**STEP, **files})


@pytest.fixture(autouse=True)
def a_utc_clock_with_weekends_off_work(settings):
    settings.OUTSEND_OPERATOR_TIMEZONE = "UTC"
    settings.OUTSEND_ENFORCE_WEEKEND_PAUSE = True


@pytest.fixture(autouse=True)
def nothing_leaves_the_machine():
    with patch("openoutsend.emails.sender._deliver") as deliver:
        yield deliver


@pytest.fixture
def box(site_config):
    return maillog.mailbox("ada@corp.com")


@pytest.fixture
def lead():
    return LeadFactory(email=LEAD, first_name="Asha")


@pytest.fixture
def deal(box, lead):
    """A cold deal in its conversation, its one email sent."""
    return in_conversation(box, lead)


def in_conversation(box, lead, sequence: str = "cold", **options) -> Deal:
    """A deal of *sequence* resting in `conversing`, with the thread its email opened."""
    deal = DealFactory(lead=lead, sequence=sequence, mailbox=box, email_subject="Your ten leads",
                       state=CONVERSING, **options)
    deal.thread = Thread.objects.create(mailbox=box, machine=machine_name(sequence), entity=str(deal.pk))
    deal.save(update_fields=["thread"])
    maillog.outbound(box, thread=deal.thread, to=lead.email, subject="Your ten leads", body=OPENER, step=0)
    return deal


def they_wrote(deal, body="The third one, she left that company."):
    return maillog.inbound(deal.mailbox, thread=deal.thread, sender=deal.lead.email, body=body)


def november(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 11, day, hour, tzinfo=UTC)


def on(moment: datetime):
    """The clock a wait is measured against, set to *moment*."""
    return patch("openoutsend.machines.machine.now", return_value=moment)


def sent_in(deal) -> list:
    """What we sent in the deal's thread, oldest first."""
    return list(deal.thread.messages.filter(direction="out").order_by("pk"))


# ── A turn ──


def test_the_models_answer_is_sent_in_the_deals_thread(deal):
    reply = they_wrote(deal)

    with model_saying(to="conversing", message="Thanks. Where did she go?"):
        machine_of(deal).fire(deal.pk)

    _, answer = sent_in(deal)
    assert answer.body_text == "Thanks. Where did she go?"
    assert (answer.to_address, answer.subject, answer.mailbox) == (LEAD, "Re: Your ten leads", deal.mailbox)
    assert answer.in_reply_to == reply.message_id
    assert answer.step is None
    assert machine_of(deal).incoming(deal) is None


def test_an_answer_is_logged_as_a_move_from_the_conversation_to_itself(deal):
    they_wrote(deal)

    with model_saying(to="conversing", message="Thanks. Where did she go?"):
        (move,) = machine_of(deal).progress(deal.pk)

    deal.refresh_from_db()
    assert (move.from_state, move.to_state) == ("conversing", "conversing")
    assert move.decided_by == "llm:test:test"
    assert move.decision == {"to": "conversing", "message": "Thanks. Where did she go?", "outcome": None}
    assert (deal.state, deal.outcome) == (CONVERSING, "")


def test_the_model_ending_with_an_outcome_writes_it_and_sends_nothing(deal):
    they_wrote(deal, body="We already use something for this, thanks.")

    with model_saying(to="completed", outcome="has_solution"):
        move = machine_of(deal).fire(deal.pk)

    deal.refresh_from_db()
    assert (move.decided_by, move.from_state, move.to_state) == ("llm:test:test", "conversing", "completed")
    assert move.decision == {"to": "completed", "message": None, "outcome": "has_solution"}
    assert (deal.state, deal.outcome) == (COMPLETED, Outcome.HAS_SOLUTION)
    assert len(sent_in(deal)) == 1
    assert not is_suppressed(LEAD)


def test_ending_without_an_outcome_is_refused(deal):
    they_wrote(deal)
    machine = machine_of(deal)
    moves_before = len(machine.history(deal.pk))

    with pytest.raises(TransitionError, match="outcome is required when the conversation ends"):
        machine.fire(deal.pk, answer={"to": "completed"})

    deal.refresh_from_db()
    assert (deal.state, deal.outcome) == (CONVERSING, "")
    assert len(machine.history(deal.pk)) == moves_before


def test_an_outcome_that_is_not_the_models_to_give_is_refused(deal):
    """`missed`, `stopped` and `undeliverable` are written by the code that learns the fact."""
    they_wrote(deal)

    with pytest.raises(TransitionError, match="outcome"):
        machine_of(deal).fire(deal.pk, answer={"to": "completed", "outcome": "stopped"})

    assert deal.state == CONVERSING


# ── A request to stop ──


def test_unsubscribed_suppresses_the_address_and_ends_the_leads_other_open_deals(deal, box, lead, tmp_path):
    other = DealFactory(lead=lead, sequence=_folder(tmp_path, "relaunch"))
    they_wrote(deal, body="Please don't email me again.")

    with model_saying(to="completed", outcome="unsubscribed"):
        machine_of(deal).fire(deal.pk)

    deal.refresh_from_db()
    other.refresh_from_db()
    assert is_suppressed(LEAD)
    assert (deal.state, deal.outcome) == (COMPLETED, Outcome.UNSUBSCRIBED)
    assert (other.state, other.outcome) == (COMPLETED, Outcome.UNSUBSCRIBED)
    assert machine_of(other).history(other.pk)[-1].decided_by == "code"
    assert len(sent_in(deal)) == 1


def test_the_deal_that_read_the_request_is_ended_once_by_the_turns_move(deal):
    they_wrote(deal, body="Take me off your list.")

    with model_saying(to="completed", outcome="unsubscribed"):
        machine_of(deal).fire(deal.pk)

    ends = [move for move in machine_of(deal).history(deal.pk) if move.to_state == COMPLETED]
    assert [(move.decided_by, move.from_state) for move in ends] == [("llm:test:test", "conversing")]


def test_any_other_ending_leaves_the_leads_other_deals_open(deal, lead, tmp_path):
    other = DealFactory(lead=lead, sequence=_folder(tmp_path, "relaunch"))
    they_wrote(deal, body="Not for us.")

    with model_saying(to="completed", outcome="not_interested"):
        machine_of(deal).fire(deal.pk)

    assert (deal.state, other.state) == (COMPLETED, awaiting(0))
    assert not is_suppressed(LEAD)


# ── A session in the model's place ──


def test_a_session_ends_the_conversation_as_the_model_would(deal):
    they_wrote(deal, body="Booked for Tuesday.")

    move = machine_of(deal).fire(deal.pk, answer={"to": "completed", "outcome": "converted"})

    deal.refresh_from_db()
    assert (move.to_state, move.decided_by) == ("completed", "agent")
    assert deal.outcome == Outcome.CONVERTED
    assert len(sent_in(deal)) == 1


# ── Silence ──


def test_silence_closes_it_unresponsive_after_the_built_in_gap(deal):
    """Five business days from Friday the 13th run out as Friday the 20th begins."""
    put_in_state(deal, CONVERSING, at=november(13))
    machine = machine_of(deal)

    with on(november(19, 23)):
        assert machine.fire(deal.pk) is None
    with on(november(20, 1)):
        move = machine.fire(deal.pk)

    deal.refresh_from_db()
    assert (move.from_state, move.to_state, move.decided_by) == ("conversing", "completed", "code")
    assert (deal.state, deal.outcome) == (COMPLETED, Outcome.UNRESPONSIVE)
    assert not is_suppressed(LEAD)
    assert len(sent_in(deal)) == 1


def test_a_folders_own_gap_closes_its_conversation(box, lead, tmp_path):
    """Seven business days from Friday the 13th run out as Tuesday the 24th begins."""
    name = _folder(tmp_path, "relaunch", **{"conversation-7d.prompt.j2": "Answer them."})
    deal = in_conversation(box, lead, name)
    put_in_state(deal, CONVERSING, at=november(13))
    machine = machine_of(deal)

    with on(november(23, 23)):
        assert machine.fire(deal.pk) is None
    with on(november(24, 1)):
        assert machine.fire(deal.pk).to_state == "completed"

    assert Deal.objects.get(pk=deal.pk).outcome == Outcome.UNRESPONSIVE


def test_a_folders_prompt_naming_no_gap_closes_after_the_built_in_one(box, lead, tmp_path):
    name = _folder(tmp_path, "relaunch", **{"conversation.prompt.j2": "Answer them."})
    deal = in_conversation(box, lead, name)
    put_in_state(deal, CONVERSING, at=november(13))
    machine = machine_of(deal)

    with on(november(19, 23)):
        assert machine.fire(deal.pk) is None
    with on(november(20, 1)):
        assert machine.fire(deal.pk).to_state == "completed"


# ── What the model answers from ──


def given_to_the_model(deal) -> str:
    """The thread and the prompt the turn's model is given, written out, the model stubbed
    to answer. The thread reaches the model as its message history (``machines/test_machines.py``)."""
    given = []

    def answer(turn, prompt, entity, schema):
        given.append(ModelTurn.written_out(entity, prompt))
        return schema.model_validate({"to": "conversing", "message": "Noted."})

    with model_saying(), patch.object(ModelTurn, "ask", autospec=True, side_effect=answer):
        machine_of(deal).fire(deal.pk)
    (prompt,) = given
    return prompt


def test_the_model_is_given_the_frame_the_whole_thread_and_the_built_in_prompt(deal):
    they_wrote(deal, body="The third one, she left that company.")

    prompt = given_to_the_model(deal)

    assert render_frame(deal) in prompt
    assert OPENER in prompt
    assert "THEY WROTE:\n[" in prompt and "]\nThe third one, she left that company." in prompt
    assert prompt.endswith(conversing.BUILT_IN_PROMPT)
    assert prompt.index(OPENER) < prompt.index("THEY WROTE:") < prompt.index(render_frame(deal))


def test_a_folders_conversation_prompt_is_rendered_with_the_deals_fields(box, lead, tmp_path):
    name = _folder(tmp_path, "relaunch", **{"conversation-7d.prompt.j2": OWN_PROMPT})
    deal = in_conversation(box, lead, name, fields={"product": "payroll audits"})
    they_wrote(deal)

    prompt = given_to_the_model(deal)

    assert render_frame(deal) in prompt
    assert OPENER in prompt and "THEY WROTE:" in prompt
    assert prompt.endswith("Answer Asha about payroll audits. End it once they name a country.")
    assert conversing.BUILT_IN_PROMPT not in prompt


def test_a_session_is_briefed_with_what_the_model_would_be_given(deal):
    they_wrote(deal)

    brief = machine_of(deal).brief(deal.pk)

    assert brief.prompt == given_to_the_model(deal)
    assert [value for value, _ in brief.choices] == ["conversing", "completed"]
    assert "outcome" in brief.schema["properties"]


def test_the_answer_records_the_prompt_it_was_written_from(deal):
    they_wrote(deal)

    with model_saying(to="conversing", message="Where did she go?"):
        machine_of(deal).fire(deal.pk)

    assert sent_in(deal)[-1].prompt == conversing.prompt_for(deal, sequences.cold())


def test_a_field_the_folders_prompt_names_and_the_deal_lacks_stops_the_turn(box, lead, tmp_path):
    name = _folder(tmp_path, "relaunch", **{"conversation-7d.prompt.j2": OWN_PROMPT})
    deal = in_conversation(box, lead, name)
    they_wrote(deal)

    with model_saying(to="conversing", message="Noted."), pytest.raises(SequenceError, match="product"):
        machine_of(deal).fire(deal.pk)

    assert deal.state == CONVERSING
    assert len(sent_in(deal)) == 1
