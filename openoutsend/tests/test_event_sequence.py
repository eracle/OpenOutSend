"""An event sequence, end to end: steps on their own days, in one thread, then the conversation.

The cold sequence's twin (``test_cold_sequence.py``): one lead in through ingest with
fields of its own, `send` on later days, only the model and SMTP stubbed. A reply never
stops a step: it is answered in the conversation the deal rests in after the last one.
"""
import io
import json
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from django.utils import timezone
from pydantic_ai.models.test import TestModel

from openoutsend.core.agents.outreach import Email
from openoutsend.core.sequences import COLD, key, named
from openoutsend.emails.models import Direction, Mailbox, Message
from openoutsend.leads.conversing import BUILT_IN_PROMPT
from openoutsend.leads.ingest import ingest
from openoutsend.leads.lifecycle import opening_step
from openoutsend.leads.models import Deal, Outcome
from openoutsend.leads.states import COMPLETED, CONVERSING, awaiting
from openoutsend.send_pass import run_send_pass
from openoutsend.tests.emails import maillog
from openoutsend.tests.factories import let_days_pass

LEAD = {"lead_id": "req-412", "email": "asha@example.com", "first_name": "Asha",
        "product": "payroll audits"}

STEPS = {
    "E0-2d-which-was-wrong.prompt.j2":
        "Ask {{ first_name }} which lead was wrong. They sell {{ product }}.",
    "E1-3d-steer-the-list.prompt.j2": "Offer to steer the list for {{ product }}.",
    "E2-5d-last-one.prompt.j2": "Last note to {{ first_name }}.",
}

EVERY_STEP = [
    ("Ask Asha which lead was wrong. They sell payroll audits.", True),
    ("Offer to steer the list for payroll audits.", False),
    ("Last note to Asha.", False),
]

pytestmark = pytest.mark.django_db


def _folder(tmp_path, name: str, files: dict) -> str:
    """A sequence folder holding *files*, under the key `--sequence` gives it."""
    folder = tmp_path / name
    folder.mkdir()
    for filename, body in files.items():
        (folder / filename).write_text(body, encoding="utf-8")
    return key(str(folder))


@pytest.fixture
def sequence(tmp_path):
    """The three-step folder, as an operator keeps it, under the key `--sequence` gives it."""
    return _folder(tmp_path, "sample-to-order", STEPS)


@pytest.fixture
def written(site_config):
    """Every ``(prompt, opener)`` the model was given, with the model and SMTP stubbed."""
    maillog.mailbox("ada@corp.com", daily_limit=10)
    calls = []

    def write(deal, prompt, *, opener=False):
        calls.append((prompt, opener))
        return Email(message=f"Email {len(calls)}.", subject="Your ten leads" if opener else None)

    with patch("openoutsend.emails.mail_pass.run_mail_pass", return_value=(0, 0, 0)), \
            patch("openoutsend.core.sending_window.within_sending_window", return_value=True), \
            patch("openoutsend.leads.summaries.materialize_profile_summary_if_missing"), \
            patch("openoutsend.core.agents.outreach.write_email", side_effect=write), \
            patch("openoutsend.emails.sender._deliver"):
        yield calls


def _enrol(sequence, **overrides):
    ingest(io.StringIO(json.dumps({**LEAD, **overrides}) + "\n"), sequence=sequence)


def _two_weeks_pass():
    """Age everything a due time is read from, as if ten working days had gone by."""
    let_days_pass(14)


def _deal() -> Deal:
    return Deal.objects.get(lead__email=LEAD["email"])


def _they_wrote(body: str) -> Message:
    """The lead's reply in the deal's thread, as the mail pass leaves it."""
    deal = _deal()
    return maillog.inbound(deal.mailbox, thread=deal.thread, sender=LEAD["email"], body=body)


def model_saying(**fields):
    """The conversation's model, deciding its turn as *fields*."""
    return patch("openoutsend.core.llm.get_llm_model",
                 return_value=TestModel(custom_output_args=fields))


def _steps_sent(deal):
    """The deal's sends that were a step of its sequence, in the order they went."""
    return (deal.thread.messages
            .filter(direction=Direction.OUTBOUND, step__isnull=False).order_by("sent_at"))


def test_nothing_goes_before_the_first_step_is_due(written, sequence):
    _enrol(sequence)
    run_send_pass(sequence=sequence)

    assert written == []
    assert _deal().state == awaiting(0)


def test_each_step_goes_on_its_day_in_one_thread_and_then_the_deal_converses(written, sequence):
    """The acceptance test: the steps as before, then `conversing`, then closed on silence."""
    _enrol(sequence)

    for number in range(3):
        assert _deal().state == awaiting(number)
        _two_weeks_pass()
        run_send_pass(sequence=sequence)

    assert written == EVERY_STEP
    deal = _deal()
    assert (deal.state, deal.outcome) == (CONVERSING, "")
    sends = deal.thread.messages.filter(direction=Direction.OUTBOUND).order_by("sent_at")
    assert list(sends.values_list("step", flat=True)) == [0, 1, 2]
    assert set(sends.values_list("subject", flat=True)) == {"Your ten leads"}
    assert set(sends.values_list("sequence", flat=True)) == {sequence}
    assert [path.rsplit("/", 1)[1] for path in sends.values_list("prompt_path", flat=True)] \
        == list(STEPS)
    assert sends.first().prompt == "Ask Asha which lead was wrong. They sell payroll audits."
    assert Deal.objects.filter(thread__isnull=False).values("thread").distinct().count() == 1

    _two_weeks_pass()
    result = run_send_pass(sequence=sequence)

    deal = _deal()
    assert result.gave_up == 1
    assert (deal.state, deal.outcome) == (COMPLETED, Outcome.UNRESPONSIVE)
    assert len(written) == 3
    assert deal.thread.messages.filter(direction=Direction.OUTBOUND).count() == 3


def test_a_step_does_not_go_before_its_days(written, sequence):
    _enrol(sequence)
    _two_weeks_pass()
    run_send_pass(sequence=sequence)
    run_send_pass(sequence=sequence)

    assert len(written) == 1
    assert _deal().state == awaiting(1)


def test_the_conversation_is_not_closed_before_its_closing_gap(written, sequence):
    """A pass on the day the last step went, and one four days on, leave it conversing."""
    _enrol(sequence)
    for _ in range(3):
        _two_weeks_pass()
        run_send_pass(sequence=sequence)

    run_send_pass(sequence=sequence)
    let_days_pass(4)
    result = run_send_pass(sequence=sequence)

    assert result.gave_up == 0
    assert (_deal().state, _deal().outcome) == (CONVERSING, "")
    assert len(written) == 3


def test_a_lead_who_replied_gets_every_remaining_step_and_is_answered_in_the_conversation(
        written, sequence):
    """A reply never stops or skips a step. The steps go on their days exactly as they do
    to a silent lead, and the reply is answered once the deal reaches `conversing`."""
    _enrol(sequence)
    _two_weeks_pass()
    run_send_pass(sequence=sequence)
    _they_wrote("The third one was wrong.")

    with model_saying(to=CONVERSING, message="Thanks. What was wrong with it?"):
        result = run_send_pass(sequence=sequence)
        assert (result.answered, result.stepped, len(written)) == (0, 0, 1)
        assert _deal().state == awaiting(1)

        for _ in range(2):
            _two_weeks_pass()
            result = run_send_pass(sequence=sequence)
            assert (result.answered, result.stepped) == (0, 1)

        deal = _deal()
        assert written == EVERY_STEP
        assert list(_steps_sent(deal).values_list("step", flat=True)) == [0, 1, 2]
        assert set(_steps_sent(deal).values_list("subject", flat=True)) == {"Your ten leads"}
        assert deal.state == CONVERSING

        result = run_send_pass(sequence=sequence)

    deal = _deal()
    assert (result.answered, result.failed) == (1, 0)
    assert (deal.state, deal.outcome) == (CONVERSING, "")
    answer = deal.thread.messages.filter(direction=Direction.OUTBOUND).order_by("pk").last()
    assert (answer.step, answer.body_text) == (None, "Thanks. What was wrong with it?")
    assert len(written) == 3


def test_a_reply_after_the_last_step_is_answered_in_the_conversation(written, sequence):
    _enrol(sequence)
    for _ in range(3):
        _two_weeks_pass()
        run_send_pass(sequence=sequence)
    reply = _they_wrote("The third one was wrong.")

    with model_saying(to=CONVERSING, message="Thanks. What was wrong with it?"):
        result = run_send_pass(sequence=sequence)

    deal = _deal()
    assert (result.answered, deal.state) == (1, CONVERSING)
    answer = deal.thread.messages.filter(direction=Direction.OUTBOUND).order_by("pk").last()
    assert (answer.subject, answer.in_reply_to) == ("Re: Your ten leads", reply.message_id)
    assert answer.body_text == "Thanks. What was wrong with it?"
    assert len(written) == 3


def test_a_piped_stop_ends_the_deal(written, sequence):
    _enrol(sequence)
    _two_weeks_pass()
    run_send_pass(sequence=sequence)

    _enrol(sequence, stop=True)
    deal = _deal()
    assert (deal.state, deal.outcome) == (COMPLETED, Outcome.STOPPED)

    _two_weeks_pass()
    run_send_pass(sequence=sequence)

    assert len(written) == 1
    deal = _deal()
    assert (deal.state, deal.outcome) == (COMPLETED, Outcome.STOPPED)


def test_a_piped_stop_ends_a_deal_in_its_conversation(written, sequence):
    """From any open state: the steps have gone, and a `stop` still ends it."""
    _enrol(sequence)
    for _ in range(3):
        _two_weeks_pass()
        run_send_pass(sequence=sequence)
    assert _deal().state == CONVERSING

    _enrol(sequence, stop=True)

    deal = _deal()
    assert (deal.state, deal.outcome) == (COMPLETED, Outcome.STOPPED)


def test_a_stopped_lead_is_never_opened(written, sequence):
    _enrol(sequence, stop=True)
    _two_weeks_pass()
    run_send_pass(sequence=sequence)

    assert written == []
    deal = _deal()
    assert (deal.state, deal.outcome, deal.thread) == (COMPLETED, Outcome.STOPPED, None)


def test_a_lead_arriving_after_every_step_was_due_is_closed_unwritten(written, sequence):
    _enrol(sequence, start_at=(timezone.now() - timedelta(days=90)).isoformat())
    result = run_send_pass(sequence=sequence)

    assert written == []
    deal = _deal()
    assert result.missed == 1
    assert (deal.state, deal.outcome) == (COMPLETED, Outcome.MISSED)


def test_a_lead_arriving_after_e0s_grace_opens_with_the_next_step_on_its_own_day(written,
                                                                                tmp_path):
    """`E0` was due six days before the lead arrived and is skipped. `E1` is due ten
    working days after `E0` *was due*, not after the deal came to wait for it."""
    late = _folder(tmp_path, "late-arrival", {
        "E0-1d-first.prompt.j2": "First note to {{ first_name }}.",
        "E1-10d-second.prompt.j2": "Second note to {{ first_name }}.",
    })
    _enrol(late, start_at=(timezone.now() - timedelta(days=7)).isoformat())
    step, due = opening_step(_deal(), named(late))
    assert step.number == 1
    assert due > timezone.now()

    run_send_pass(sequence=late)

    assert written == []
    assert _deal().state == awaiting(1)

    # Twelve days after `start_at`: before `E1`'s day, which is at least fifteen after it.
    let_days_pass(5)
    run_send_pass(sequence=late)

    assert written == []
    assert _deal().state == awaiting(1)

    # Eighteen days after `start_at`, past `E1`'s day, and only eleven after the deal
    # came to wait for it: counted from there, ten working days would not have run out.
    let_days_pass(6)
    result = run_send_pass(sequence=late)

    assert result.opened == 1
    assert written == [("Second note to Asha.", True)]
    deal = _deal()
    sent = deal.thread.messages.get(direction=Direction.OUTBOUND)
    assert (sent.step, sent.subject) == (1, "Your ten leads")
    assert deal.state == CONVERSING


def test_a_bare_pass_sends_every_sequence(written, sequence):
    """`outsend send` with no `--sequence`: cold is one sequence among the install's."""
    _enrol(sequence)
    ingest(io.StringIO(json.dumps({"lead_id": "cold-1", "email": "bo@example.com",
                                   "first_name": "Bo"}) + "\n"))
    _two_weeks_pass()
    run_send_pass()
    # One box opens one conversation a pass; let its spacing clock run out.
    Mailbox.objects.update(next_send_at=None)
    run_send_pass()

    assert len(written) == 2
    assert set(Deal.objects.filter(thread__isnull=False)
               .values_list("sequence", flat=True)) == {COLD, sequence}


def test_a_pass_narrowed_to_cold_never_sends_an_event_sequence(written, sequence):
    _enrol(sequence)
    _two_weeks_pass()
    run_send_pass(sequence=COLD)

    assert written == []
    assert _deal().state == awaiting(0)


def test_an_event_pass_never_opens_a_cold_lead(written, sequence):
    _enrol(COLD)
    _two_weeks_pass()
    run_send_pass(sequence=sequence)

    assert written == []
    assert _deal().state == awaiting(0)


def test_one_lead_in_two_sequences_is_two_deals(written, sequence):
    _enrol(COLD)
    _enrol(sequence)

    assert set(Deal.objects.values_list("sequence", flat=True)) == {COLD, sequence}


def test_a_lead_in_two_sequences_gets_one_message_a_pass(written, sequence):
    """Both deals fall due together, with two free boxes: one goes now, the other next pass."""
    maillog.mailbox("bea@corp.com", daily_limit=10)
    _enrol(COLD)
    _enrol(sequence)
    _two_weeks_pass()

    run_send_pass()
    assert len(written) == 1

    run_send_pass()
    assert len(written) == 2
    assert set(Deal.objects.filter(thread__isnull=False)
               .values_list("sequence", flat=True)) == {COLD, sequence}


@contextmanager
def _model_writing(message: str):
    """Every stub the pass needs, with the model writing *message* every time. Yields the model."""
    maillog.mailbox("ada@corp.com", daily_limit=10)
    written = Email(message=message, subject="Your ten leads")
    with patch("openoutsend.emails.mail_pass.run_mail_pass", return_value=(0, 0, 0)), \
            patch("openoutsend.core.sending_window.within_sending_window", return_value=True), \
            patch("openoutsend.leads.summaries.materialize_profile_summary_if_missing"), \
            patch("openoutsend.core.agents.outreach.write_email",
                  return_value=written) as model, \
            patch("openoutsend.emails.sender._deliver"):
        yield model


def test_an_agent_drafted_opener_opens_the_sequence(written, sequence):
    from openoutsend.core.agent_draft import AgentDraft
    from openoutsend.errors import DraftPending

    _enrol(sequence)
    _two_weeks_pass()

    with pytest.raises(DraftPending):
        run_send_pass(sequence=sequence, agent_draft=AgentDraft(active=True))
    result = run_send_pass(sequence=sequence, agent_draft=AgentDraft(
        active=True, subject="Your ten leads", body="Which of the ten was wrong?"))

    assert (result.opened, written) == (1, [])
    sent = Message.objects.get(direction=Direction.OUTBOUND)
    assert (sent.step, sent.sequence) == (0, sequence)
    assert sent.body_text == "Which of the ten was wrong?"
    assert _deal().state == awaiting(1)


# ── The folder's own conversation ─────────────────────────────────


@contextmanager
def _turn_answering(message: str):
    """The conversation's model answering *message*. Yields every prompt it was given."""
    prompts = []

    def ask(turn, prompt, entity, schema):
        prompts.append(prompt)
        return schema.model_validate({"to": CONVERSING, "message": message})

    with patch("openoutsend.machines.machine._ModelTurn.ask", autospec=True, side_effect=ask), \
            patch("openoutsend.machines.machine._ModelTurn.decided_by", "llm:test:test"):
        yield prompts


def test_a_folders_conversation_prompt_is_what_the_model_answers_from(written, tmp_path):
    """`conversation-<days>d.prompt.j2`: rendered with the deal's fields, in the built-in
    prompt's place, after the whole thread."""
    own = _folder(tmp_path, "sample-to-order", {
        "E0-ten-leads.prompt.j2": "Send {{ first_name }} their ten leads.",
        "conversation-20d.prompt.j2":
            "{{ first_name }} was sent ten leads for {{ product }}. Ask which was wrong.",
    })
    _enrol(own)
    run_send_pass(sequence=own)
    _they_wrote("Two of them are already clients.")

    with _turn_answering("Which two?") as prompts:
        result = run_send_pass(sequence=own)

    (prompt,) = prompts
    assert result.answered == 1
    assert prompt.endswith("Asha was sent ten leads for payroll audits. Ask which was wrong.")
    assert "Two of them are already clients." in prompt
    assert BUILT_IN_PROMPT not in prompt
    assert BUILT_IN_PROMPT[:60] not in prompt
    answer = _deal().thread.messages.filter(direction=Direction.OUTBOUND).order_by("pk").last()
    assert answer.body_text == "Which two?"


def test_a_folder_without_one_gets_the_built_in_prompt(written, sequence):
    _enrol(sequence)
    for _ in range(3):
        _two_weeks_pass()
        run_send_pass(sequence=sequence)
    _they_wrote("Two of them are already clients.")

    with _turn_answering("Which two?") as prompts:
        run_send_pass(sequence=sequence)

    (prompt,) = prompts
    assert prompt.endswith(BUILT_IN_PROMPT)


def test_a_folders_conversation_days_are_its_closing_gap(written, tmp_path):
    """`conversation-20d`: two weeks of silence, which close the built-in conversation,
    leave this one open; twenty working days close it."""
    own = _folder(tmp_path, "sample-to-order", {
        "E0-ten-leads.prompt.j2": "Send {{ first_name }} their ten leads.",
        "conversation-20d.prompt.j2": "Ask {{ first_name }} which was wrong.",
    })
    assert named(own).conversation.days == 20
    _enrol(own)
    run_send_pass(sequence=own)

    let_days_pass(14)
    result = run_send_pass(sequence=own)

    assert result.gave_up == 0
    assert _deal().state == CONVERSING

    let_days_pass(15)
    result = run_send_pass(sequence=own)

    assert result.gave_up == 1
    assert (_deal().state, _deal().outcome) == (COMPLETED, Outcome.UNRESPONSIVE)


def test_a_short_closing_gap_closes_sooner_than_the_built_in_one(written, tmp_path):
    """`conversation-1d`: three days on, one working day has gone by and five have not."""
    own = _folder(tmp_path, "sample-to-order", {
        "E0-ten-leads.prompt.j2": "Send {{ first_name }} their ten leads.",
        "conversation-1d.prompt.j2": "Ask {{ first_name }} which was wrong.",
    })
    _enrol(own)
    _enrol(COLD, lead_id="cold-1", email="bo@example.com")
    run_send_pass()
    Mailbox.objects.update(next_send_at=None)
    run_send_pass()
    assert set(Deal.objects.values_list("thread", flat=True)) != {None}

    let_days_pass(3)
    result = run_send_pass()

    assert result.gave_up == 1
    assert (_deal().state, _deal().outcome) == (COMPLETED, Outcome.UNRESPONSIVE)
    assert Deal.objects.get(sequence=COLD).state == CONVERSING


# ── The shipped example, examples/sequences/after-a-demo ──────────

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "sequences" / "after-a-demo"


def _example_line(**overrides) -> str:
    line = json.loads((EXAMPLE / "leads.jsonl").read_text(encoding="utf-8"))
    return json.dumps({**line, "start_at": timezone.now().isoformat(), **overrides}) + "\n"


def test_the_example_sends_its_three_steps_from_the_piped_fields(site_config):
    after_a_demo = key(str(EXAMPLE))
    ingest(io.StringIO(_example_line()), sequence=after_a_demo)

    with _model_writing("Thanks for the demo.") as model:
        for _ in range(3):
            _two_weeks_pass()
            run_send_pass(sequence=after_a_demo)

    prompts = [call.args[1] for call in model.call_args_list]
    assert "Runs a 6-person support team" in prompts[0]
    assert "Can it import the last year of tickets?" in prompts[1]
    assert "https://cal.example.com/sam" in prompts[2]
    sends = Message.objects.filter(direction=Direction.OUTBOUND, sequence=after_a_demo)
    assert list(sends.order_by("sent_at").values_list("step", flat=True)) == [0, 1, 2]
    assert Deal.objects.get(sequence=after_a_demo).state == CONVERSING


def test_the_example_stops_when_the_line_is_piped_with_stop(site_config):
    after_a_demo = key(str(EXAMPLE))
    ingest(io.StringIO(_example_line()), sequence=after_a_demo)

    with _model_writing("Thanks for the demo.") as model:
        _two_weeks_pass()
        run_send_pass(sequence=after_a_demo)
        ingest(io.StringIO(_example_line(stop=True)), sequence=after_a_demo)
        _two_weeks_pass()
        run_send_pass(sequence=after_a_demo)

    assert model.call_count == 1
    deal = Deal.objects.get(sequence=after_a_demo)
    assert (deal.state, deal.outcome) == (COMPLETED, Outcome.STOPPED)


# ── Email steps: sent as written ──────────────────────────────────


EMAILS = {
    "E0-ten-free.email.j2": "{# R1 #}\nSubject: Ten leads, free\n\nHi,\n\nTen free, {{ product }}.",
    "E1-4d-what-it-looks-like.email.j2": "Hi again,\n\nHere is one for {{ product }}.",
}


@pytest.fixture
def emails(tmp_path):
    """A two-step folder of email steps, under the key `--sequence` gives it."""
    return _folder(tmp_path, "relaunch", EMAILS)


def test_email_steps_go_as_written_with_no_model_call(written, emails):
    _enrol(emails)

    for _ in range(2):
        _two_weeks_pass()
        run_send_pass(sequence=emails)

    assert written == []
    sends = _deal().thread.messages.filter(direction=Direction.OUTBOUND).order_by("sent_at")
    assert list(sends.values_list("subject", flat=True)) == ["Ten leads, free"] * 2
    assert sends[0].body_text == "Hi,\n\nTen free, payroll audits."
    assert sends[1].body_text == "Hi again,\n\nHere is one for payroll audits."
    assert set(sends.values_list("prompt", flat=True)) == {""}
    assert _deal().state == CONVERSING


def test_an_agent_drafting_pass_sends_email_steps_instead_of_handing_them_back(written,
                                                                               emails):
    from openoutsend.core.agent_draft import AgentDraft

    _enrol(emails)
    _two_weeks_pass()
    result = run_send_pass(sequence=emails, agent_draft=AgentDraft(active=True))

    assert written == []
    assert result.opened == 1
    assert list(_steps_sent(_deal()).values_list("step", flat=True)) == [0]
    assert _deal().state == awaiting(1)
