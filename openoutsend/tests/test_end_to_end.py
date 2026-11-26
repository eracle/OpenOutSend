"""Sequences end to end: one lead in through ingest, `send` on later days, as an operator
runs it, with only the model and SMTP stubbed.

Every piece is tested beside its own code (the deal machine in ``leads/test_lifecycle.py``,
its conversation in ``leads/test_conversing.py``, the pass in ``test_send_pass.py``); this
is the whole of it through the pipe and the pass. The built-in cold sequence is one
opener and then the conversation; an event sequence is steps on their own days in one
thread, then the conversation. A reply never stops a step.
"""
import io
import json
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from django.utils import timezone

from openoutsend.core.agents.outreach import Email
from openoutsend.core.sequences import COLD, key, named
from openoutsend.emails.models import Direction, Mailbox, Message
from openoutsend.leads.ingest import ingest
from openoutsend.leads.lifecycle import opening_step
from openoutsend.leads.models import Deal, Outcome
from openoutsend.leads.states import COMPLETED, CONVERSING, awaiting
from openoutsend.send_pass import run_send_pass
from openoutsend.tests.emails import maillog
from openoutsend.tests.factories import let_days_pass, model_saying, sequence_folder

LEAD = {"lead_id": "req-412", "email": "asha@example.com", "first_name": "Asha",
        "product": "payroll audits"}

SUBJECT = "Your ten leads"

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


@pytest.fixture
def sequence(tmp_path):
    """The three-step folder, as an operator keeps it, under the key `--sequence` gives it."""
    return sequence_folder(tmp_path, "sample-to-order", STEPS)


@contextmanager
def _stubbed(write):
    """Every stub a pass needs, with *write* standing in for the step's model."""
    with patch("openoutsend.emails.mail_pass.run_mail_pass", return_value=(0, 0, 0)), \
            patch("openoutsend.core.sending_window.within_sending_window", return_value=True), \
            patch("openoutsend.leads.summaries.materialize_profile_summary_if_missing"), \
            patch("openoutsend.core.agents.outreach.write_email", side_effect=write) as model, \
            patch("openoutsend.emails.sender._deliver"):
        yield model


@pytest.fixture
def written(site_config):
    """Every ``(prompt, opener)`` the step's model was given, in order."""
    maillog.mailbox("ada@corp.com", daily_limit=10)
    calls = []

    def write(deal, prompt, *, opener=False):
        calls.append((prompt, opener))
        return Email(message=f"Email {len(calls)}.", subject=SUBJECT if opener else None)

    with _stubbed(write):
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


def _sent(deal) -> list[str]:
    return list(deal.thread.messages.filter(direction=Direction.OUTBOUND)
                .order_by("pk").values_list("body_text", flat=True))


def _steps_sent(deal):
    """The deal's sends that were a step of its sequence, in the order they went."""
    return (deal.thread.messages
            .filter(direction=Direction.OUTBOUND, step__isnull=False).order_by("sent_at"))


# ── The cold sequence: an opener, then the conversation ───────────


def test_a_silent_lead_gets_the_opener_and_is_closed_once_the_silence_has_run_out(written):
    _enrol(COLD)
    result = run_send_pass()

    deal = _deal()
    assert (result.opened, len(written)) == (1, 1)
    assert (deal.sequence, deal.state, deal.outcome) == ("cold", CONVERSING, "")
    assert _sent(deal) == ["Email 1."]

    let_days_pass(14)
    result = run_send_pass()

    deal = _deal()
    assert result.gave_up == 1
    assert (deal.state, deal.outcome) == (COMPLETED, Outcome.UNRESPONSIVE)
    assert len(written) == 1
    assert _sent(deal) == ["Email 1."]


def test_a_pass_on_the_same_day_sends_nothing_more(written):
    _enrol(COLD)
    run_send_pass()
    result = run_send_pass()

    assert len(written) == 1
    assert (result.opened, result.stepped, result.answered, result.gave_up) == (0, 0, 0, 0)
    assert _deal().state == CONVERSING


def test_a_lead_who_replies_is_answered_by_the_models_turn(written):
    _enrol(COLD)
    run_send_pass()
    reply = _they_wrote("We do this by hand today.")

    with model_saying(to=CONVERSING, message="What does by hand look like?"):
        result = run_send_pass()

    deal = _deal()
    assert (result.answered, result.ended, result.failed) == (1, 0, 0)
    assert (deal.state, deal.outcome) == (CONVERSING, "")
    assert _sent(deal) == ["Email 1.", "What does by hand look like?"]
    answer = deal.thread.messages.order_by("pk").last()
    assert (answer.subject, answer.in_reply_to) == (f"Re: {SUBJECT}", reply.message_id)
    assert len(written) == 1


def test_an_answered_lead_is_not_answered_twice_and_silence_then_closes_it(written):
    _enrol(COLD)
    run_send_pass()
    _they_wrote("We do this by hand today.")

    with model_saying(to=CONVERSING, message="What does by hand look like?"):
        run_send_pass()
        result = run_send_pass()
        assert result.answered == 0

        let_days_pass(14)
        result = run_send_pass()

    deal = _deal()
    assert result.gave_up == 1
    assert (deal.state, deal.outcome) == (COMPLETED, Outcome.UNRESPONSIVE)
    assert len(_sent(deal)) == 2


def test_a_lead_owed_an_answer_is_not_closed_for_silence(written):
    """The answer failed for two weeks: the lead is still owed it, so the deal stays open."""
    _enrol(COLD)
    run_send_pass()
    _they_wrote("We do this by hand today.")
    let_days_pass(14)

    with patch("openoutsend.machines.handlers.ModelTurn.ask", side_effect=RuntimeError("model down")):
        result = run_send_pass()

    assert (result.answered, result.gave_up, result.failed) == (0, 0, 1)
    assert _deal().state == CONVERSING


def test_the_model_ends_the_conversation_and_says_why(written):
    _enrol(COLD)
    run_send_pass()
    _they_wrote("Not for us, thanks.")

    with model_saying(to=COMPLETED, outcome="not_interested"):
        result = run_send_pass()

    deal = _deal()
    assert (result.answered, result.ended) == (0, 1)
    assert (deal.state, deal.outcome) == (COMPLETED, Outcome.NOT_INTERESTED)
    assert _sent(deal) == ["Email 1."]


# ── An event sequence: steps on their days, in one thread ─────────


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
    assert set(sends.values_list("subject", flat=True)) == {SUBJECT}
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
        assert set(_steps_sent(deal).values_list("subject", flat=True)) == {SUBJECT}
        assert deal.state == CONVERSING

        result = run_send_pass(sequence=sequence)

    deal = _deal()
    assert (result.answered, result.failed) == (1, 0)
    assert (deal.state, deal.outcome) == (CONVERSING, "")
    answer = deal.thread.messages.filter(direction=Direction.OUTBOUND).order_by("pk").last()
    assert (answer.step, answer.body_text) == (None, "Thanks. What was wrong with it?")
    assert answer.subject == f"Re: {SUBJECT}"
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


def test_a_lead_arriving_after_e0s_grace_opens_with_the_next_step_on_its_own_day(written,
                                                                                tmp_path):
    """`E0` was due six days before the lead arrived and is skipped. `E1` is due ten
    working days after `E0` *was due*, not after the deal came to wait for it."""
    late = sequence_folder(tmp_path, "late-arrival", {
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
    assert (sent.step, sent.subject) == (1, SUBJECT)
    assert deal.state == CONVERSING


# ── Several sequences in one install ──────────────────────────────


def test_a_bare_pass_sends_every_sequence(written, sequence):
    """`outsend send` with no `--sequence`: cold is one sequence among the install's."""
    _enrol(sequence)
    _enrol(COLD, lead_id="cold-1", email="bo@example.com")
    _two_weeks_pass()
    run_send_pass()
    # One box opens one conversation a pass; let its spacing clock run out.
    Mailbox.objects.update(next_send_at=None)
    run_send_pass()

    assert len(written) == 2
    assert set(Deal.objects.filter(thread__isnull=False)
               .values_list("sequence", flat=True)) == {COLD, sequence}


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


# ── The shipped example, examples/sequences/after-a-demo ──────────

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "sequences" / "after-a-demo"


def test_the_example_sends_its_three_steps_from_the_piped_fields(site_config):
    after_a_demo = key(str(EXAMPLE))
    line = json.loads((EXAMPLE / "leads.jsonl").read_text(encoding="utf-8"))
    ingest(io.StringIO(json.dumps({**line, "start_at": timezone.now().isoformat()}) + "\n"),
           sequence=after_a_demo)
    maillog.mailbox("ada@corp.com", daily_limit=10)

    with _stubbed(lambda deal, prompt, opener=False: Email(message="Thanks for the demo.",
                                                           subject=SUBJECT)) as model:
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


# ── Email steps: sent as written ──────────────────────────────────


EMAILS = {
    "E0-ten-free.email.j2": "{# R1 #}\nSubject: Ten leads, free\n\nHi,\n\nTen free, {{ product }}.",
    "E1-4d-what-it-looks-like.email.j2": "Hi again,\n\nHere is one for {{ product }}.",
}


@pytest.fixture
def emails(tmp_path):
    """A two-step folder of email steps, under the key `--sequence` gives it."""
    return sequence_folder(tmp_path, "relaunch", EMAILS)


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
