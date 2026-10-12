"""An event sequence, end to end: steps on their own days, in one thread, until the last.

The cold sequence's twin (``test_cold_sequence.py``): one lead in through ingest with
fields of its own, `send` on later days, only the model and SMTP stubbed. Follow-ups are
off, as an install mailing an event sequence runs them.
"""
import io
import json
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.db.models import F
from django.utils import timezone

from openoutsend.core.agents.outreach import OutreachDecision
from openoutsend.core.sequences import COLD, key
from openoutsend.emails.models import Direction, Mailbox, Message
from openoutsend.leads.ingest import ingest
from openoutsend.leads.models import Deal, DealState, Outcome
from openoutsend.send_pass import run_send_pass
from openoutsend.tests.emails import maillog

LEAD = {"lead_id": "req-412", "email": "asha@example.com", "first_name": "Asha",
        "product": "payroll audits"}

STEPS = {
    "E0-2d-which-was-wrong.md": "Ask {{ first_name }} which lead was wrong. They sell {{ product }}.",
    "E1-3d-steer-the-list.md": "Offer to steer the list for {{ product }}.",
    "E2-5d-last-one.md": "Last note to {{ first_name }}.",
}

pytestmark = pytest.mark.django_db


@pytest.fixture
def sequence(tmp_path):
    """The three-step folder, as an operator keeps it, under the key `--sequence` gives it."""
    folder = tmp_path / "sample-to-order"
    folder.mkdir()
    for name, body in STEPS.items():
        (folder / name).write_text(body, encoding="utf-8")
    return key(str(folder))


@pytest.fixture
def written(site_config, monkeypatch):
    """Every ``(prompt, opener)`` the model was given, with the model and SMTP stubbed."""
    monkeypatch.setenv("OUTSEND_FOLLOW_UPS", "off")
    maillog.mailbox("ada@corp.com", daily_limit=10)
    calls = []

    def write(deal, prompt, *, opener=False):
        calls.append((prompt, opener))
        return OutreachDecision(action="send_message", message=f"Email {len(calls)}.",
                                subject="Your ten leads" if opener else None)

    with patch("openoutsend.emails.mail_pass.run_mail_pass", return_value=(0, 0, 0)), \
            patch("openoutsend.core.sending_window.within_sending_window", return_value=True), \
            patch("openoutsend.leads.summaries.materialize_profile_summary_if_missing"), \
            patch("openoutsend.core.agents.outreach.run_outreach_agent", side_effect=write), \
            patch("openoutsend.emails.sender._deliver"):
        yield calls


def _enrol(sequence, **overrides):
    ingest(io.StringIO(json.dumps({**LEAD, **overrides}) + "\n"), sequence=sequence)


def _two_weeks_pass():
    """Age everything a due time is read from, as if ten working days had gone by."""
    gone = timedelta(days=14)
    Message.objects.update(sent_at=F("sent_at") - gone)
    Deal.objects.update(start_at=F("start_at") - gone, created_at=F("created_at") - gone)
    Mailbox.objects.update(next_send_at=None)


def _deal() -> Deal:
    return Deal.objects.get(lead__email=LEAD["email"])


def test_nothing_goes_before_the_first_step_is_due(written, sequence):
    _enrol(sequence)
    run_send_pass(sequence=sequence)

    assert written == []
    assert _deal().state == DealState.READY


def test_each_step_goes_on_its_day_in_one_thread_and_then_it_ends(written, sequence):
    _enrol(sequence)

    for _ in range(3):
        _two_weeks_pass()
        run_send_pass(sequence=sequence)

    assert written == [
        ("Ask Asha which lead was wrong. They sell payroll audits.", True),
        ("Offer to steer the list for payroll audits.", False),
        ("Last note to Asha.", False),
    ]
    deal = _deal()
    assert deal.step == 2
    sends = deal.thread.messages.filter(direction=Direction.OUTBOUND).order_by("sent_at")
    assert list(sends.values_list("step", flat=True)) == [0, 1, 2]
    assert set(sends.values_list("subject", flat=True)) == {"Your ten leads"}
    assert set(sends.values_list("sequence", flat=True)) == {sequence}
    assert [path.rsplit("/", 1)[1] for path in sends.values_list("prompt_path", flat=True)] \
        == list(STEPS)
    assert sends.first().prompt == "Ask Asha which lead was wrong. They sell payroll audits."

    _two_weeks_pass()
    run_send_pass(sequence=sequence)

    deal = _deal()
    assert (deal.state, deal.outcome) == (DealState.COMPLETED, Outcome.UNRESPONSIVE)
    assert len(written) == 3


def test_a_step_does_not_go_before_its_days(written, sequence):
    _enrol(sequence)
    _two_weeks_pass()
    run_send_pass(sequence=sequence)
    run_send_pass(sequence=sequence)

    assert len(written) == 1


def test_a_piped_stop_ends_the_steps(written, sequence):
    _enrol(sequence)
    _two_weeks_pass()
    run_send_pass(sequence=sequence)

    _enrol(sequence, stop=True)
    _two_weeks_pass()
    run_send_pass(sequence=sequence)

    assert len(written) == 1
    assert _deal().state == DealState.EMAILED


def test_a_stopped_lead_is_never_opened(written, sequence):
    _enrol(sequence, stop=True)
    _two_weeks_pass()
    run_send_pass(sequence=sequence)

    assert written == []


def test_a_lead_arriving_after_every_step_was_due_is_closed_unwritten(written, sequence):
    _enrol(sequence, start_at=(timezone.now() - timedelta(days=90)).isoformat())
    run_send_pass(sequence=sequence)

    assert written == []
    deal = _deal()
    assert (deal.state, deal.outcome) == (DealState.COMPLETED, Outcome.MISSED)


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
    assert _deal().state == DealState.READY


def test_an_event_pass_never_opens_a_cold_lead(written, sequence):
    _enrol(COLD)
    _two_weeks_pass()
    run_send_pass(sequence=sequence)

    assert written == []
    assert _deal().state == DealState.READY


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


def test_a_placeholder_the_model_wrote_is_filled_before_it_goes(sequence, site_config):
    _enrol(sequence, order_url="https://buy.example.com/412")
    _two_weeks_pass()

    with _model_writing("Order here: {order_url}"):
        run_send_pass(sequence=sequence)

    sent = Message.objects.get(direction=Direction.OUTBOUND)
    assert sent.body_text == "Order here: https://buy.example.com/412"


def test_a_draft_failing_twice_is_held_until_the_line_is_piped_again(sequence, site_config):
    _enrol(sequence)
    _two_weeks_pass()

    with _model_writing("Order here: {order_url}") as calls:
        result = run_send_pass(sequence=sequence)
        assert (result.held, calls.call_count) == (1, 2)
        assert _deal().held == "unknown placeholder {order_url}"

        run_send_pass(sequence=sequence)
        assert calls.call_count == 2

        _enrol(sequence, order_url="https://buy.example.com/412")
        result = run_send_pass(sequence=sequence)

    assert (result.opened, result.held) == (1, 0)
    assert _deal().held == ""


def test_a_banned_phrase_is_never_sent(sequence, site_config, tmp_path, monkeypatch):
    banned = tmp_path / "banned.txt"
    banned.write_text("circle back\n", encoding="utf-8")
    monkeypatch.setenv("OUTSEND_BANNED_PHRASES", str(banned))
    _enrol(sequence)
    _two_weeks_pass()

    with _model_writing("Happy to Circle Back."):
        run_send_pass(sequence=sequence)

    assert not Message.objects.filter(direction=Direction.OUTBOUND).exists()
    assert _deal().held == 'banned phrase "circle back"'


@contextmanager
def _model_writing(message: str):
    """Every stub the pass needs, with the model writing *message* every time. Yields the model."""
    maillog.mailbox("ada@corp.com", daily_limit=10)
    written = OutreachDecision(action="send_message", message=message, subject="Your ten leads")
    with patch("openoutsend.emails.mail_pass.run_mail_pass", return_value=(0, 0, 0)), \
            patch("openoutsend.core.sending_window.within_sending_window", return_value=True), \
            patch("openoutsend.leads.summaries.materialize_profile_summary_if_missing"), \
            patch("openoutsend.core.agents.outreach.run_outreach_agent",
                  return_value=written) as model, \
            patch("openoutsend.emails.sender._deliver"):
        yield model


def test_a_reply_stops_the_remaining_steps(written, sequence):
    _enrol(sequence)
    _two_weeks_pass()
    run_send_pass(sequence=sequence)
    deal = _deal()
    maillog.inbound(deal.mailbox, thread=deal.thread, sender=LEAD["email"])

    with patch("openoutsend.emails.steps.reply.answer_reply", return_value=None) as answer:
        _two_weeks_pass()
        run_send_pass(sequence=sequence)

    answer.assert_called_once()
    assert len(written) == 1


def test_an_agent_drafted_opener_opens_the_sequence_with_its_placeholders_filled(
        written, sequence):
    from openoutsend.core.agent_draft import AgentDraft
    from openoutsend.errors import DraftPending

    _enrol(sequence, order_url="https://buy.example.com/412")
    _two_weeks_pass()

    with pytest.raises(DraftPending):
        run_send_pass(sequence=sequence, agent_draft=AgentDraft(active=True))
    result = run_send_pass(sequence=sequence, agent_draft=AgentDraft(
        active=True, subject="Your ten leads", body="Order here: {order_url}"))

    assert (result.opened, written) == (1, [])
    sent = Message.objects.get(direction=Direction.OUTBOUND)
    assert (sent.step, sent.sequence) == (0, sequence)
    assert sent.body_text == "Order here: https://buy.example.com/412"


# ── OUTSEND_REPLIES=person ────────────────────────────────────────


@pytest.fixture
def by_person(monkeypatch):
    monkeypatch.setenv("OUTSEND_REPLIES", "person")


def _first_step_then_a_reply(sequence, text: str):
    _enrol(sequence)
    _two_weeks_pass()
    run_send_pass(sequence=sequence)
    deal = _deal()
    maillog.inbound(deal.mailbox, thread=deal.thread, sender=LEAD["email"], body=text)


def test_with_a_person_answering_a_reply_is_reported_once_and_the_steps_go_on(
        written, sequence, by_person):
    _first_step_then_a_reply(sequence, "Which country were these from?")

    with patch("openoutsend.core.agents.opt_out.asks_to_stop", return_value=False) as judged:
        reported = run_send_pass(sequence=sequence)
        again = run_send_pass(sequence=sequence)
        _two_weeks_pass()
        run_send_pass(sequence=sequence)

    assert reported.for_a_person == [f"{LEAD['email']} — Your ten leads"]
    assert again.for_a_person == []
    judged.assert_called_once()
    assert [prompt for prompt, _ in written] == [
        "Ask Asha which lead was wrong. They sell payroll audits.",
        "Offer to steer the list for payroll audits.",
    ]


def test_with_a_person_answering_a_worded_unsubscribe_is_still_honoured(
        written, sequence, by_person):
    from openoutsend.leads.suppression import is_suppressed

    _first_step_then_a_reply(sequence, "Please take me off your list.")

    with patch("openoutsend.core.agents.opt_out.asks_to_stop", return_value=True):
        result = run_send_pass(sequence=sequence)
    _two_weeks_pass()
    run_send_pass(sequence=sequence)

    assert result.for_a_person == []
    assert is_suppressed(LEAD["email"])
    assert (_deal().state, _deal().outcome) == (DealState.COMPLETED, Outcome.UNSUBSCRIBED)
    assert len(written) == 1


def test_an_unknown_replies_setting_is_an_error(monkeypatch):
    from openoutsend.core.conf import replies_by
    from openoutsend.errors import OutsendError

    monkeypatch.setenv("OUTSEND_REPLIES", "robot")

    with pytest.raises(OutsendError, match="OUTSEND_REPLIES"):
        replies_by()
