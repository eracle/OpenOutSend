"""The built-in cold sequence, end to end: an opener, then the conversation, then the end.

Every piece is tested beside its own code; this is the whole of it through the pipe and
the pass, as an operator runs it — one lead in through ingest, `send` on later days —
with only the model and SMTP stubbed.
"""
import io
import json
from unittest.mock import patch

import pytest
from pydantic_ai.models.test import TestModel

from openoutsend.core.agents.outreach import Email
from openoutsend.emails.models import Direction
from openoutsend.leads.ingest import ingest
from openoutsend.leads.models import Deal, Outcome
from openoutsend.leads.states import COMPLETED, CONVERSING
from openoutsend.send_pass import run_send_pass
from openoutsend.tests.emails import maillog
from openoutsend.tests.factories import let_days_pass

LEAD = {"lead_id": "lead-1", "email": "sam@example.com", "first_name": "Sam",
        "company": "Example Co"}

pytestmark = pytest.mark.django_db


@pytest.fixture
def written(site_config):
    """Every prompt the model was given, in order, with the model and SMTP stubbed."""
    maillog.mailbox("ada@corp.com", daily_limit=10)
    prompts = []

    def write(deal, prompt, *, opener=False):
        prompts.append(prompt)
        return Email(message=f"Email {len(prompts)}.", subject="A question" if opener else None)

    with patch("openoutsend.emails.mail_pass.run_mail_pass", return_value=(0, 0, 0)), \
            patch("openoutsend.core.sending_window.within_sending_window", return_value=True), \
            patch("openoutsend.leads.summaries.materialize_profile_summary_if_missing"), \
            patch("openoutsend.core.agents.outreach.write_email", side_effect=write), \
            patch("openoutsend.emails.sender._deliver"):
        ingest(io.StringIO(json.dumps(LEAD) + "\n"))
        yield prompts


def _deal() -> Deal:
    return Deal.objects.get(lead__email=LEAD["email"])


def _they_wrote(body: str):
    """The lead's reply in the deal's thread, as the mail pass leaves it."""
    deal = _deal()
    return maillog.inbound(deal.mailbox, thread=deal.thread, sender=LEAD["email"], body=body)


def model_saying(**fields):
    """The conversation's model, deciding its turn as *fields*."""
    return patch("openoutsend.core.llm.get_llm_model",
                 return_value=TestModel(custom_output_args=fields))


def _sent(deal) -> list[str]:
    return list(deal.thread.messages.filter(direction=Direction.OUTBOUND)
                .order_by("pk").values_list("body_text", flat=True))


def test_a_silent_lead_gets_the_opener_and_is_closed_once_the_silence_has_run_out(written):
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
    run_send_pass()
    result = run_send_pass()

    assert len(written) == 1
    assert (result.opened, result.stepped, result.answered, result.gave_up) == (0, 0, 0, 0)
    assert _deal().state == CONVERSING


def test_the_conversation_stays_open_until_its_closing_gap_has_run_out(written):
    """Five working days of silence close it; four do not."""
    run_send_pass()
    let_days_pass(4)
    run_send_pass()

    assert _deal().state == CONVERSING

    let_days_pass(4)
    run_send_pass()

    assert (_deal().state, _deal().outcome) == (COMPLETED, Outcome.UNRESPONSIVE)


def test_a_lead_who_replies_is_answered_by_the_models_turn(written):
    run_send_pass()
    reply = _they_wrote("We do this by hand today.")

    with model_saying(to=CONVERSING, message="What does by hand look like?"):
        result = run_send_pass()

    deal = _deal()
    assert (result.answered, result.ended, result.failed) == (1, 0, 0)
    assert (deal.state, deal.outcome) == (CONVERSING, "")
    assert _sent(deal) == ["Email 1.", "What does by hand look like?"]
    answer = deal.thread.messages.order_by("pk").last()
    assert (answer.subject, answer.in_reply_to) == ("Re: A question", reply.message_id)
    assert len(written) == 1


def test_an_answered_lead_is_not_answered_twice_and_silence_then_closes_it(written):
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
    run_send_pass()
    _they_wrote("We do this by hand today.")
    let_days_pass(14)

    with patch("openoutsend.machines.steps.ModelTurn.ask", side_effect=RuntimeError("model down")):
        result = run_send_pass()

    assert (result.answered, result.gave_up, result.failed) == (0, 0, 1)
    assert _deal().state == CONVERSING


def test_the_model_ends_the_conversation_and_says_why(written):
    run_send_pass()
    _they_wrote("Not for us, thanks.")

    with model_saying(to=COMPLETED, outcome="not_interested"):
        result = run_send_pass()

    deal = _deal()
    assert (result.answered, result.ended) == (0, 1)
    assert (deal.state, deal.outcome) == (COMPLETED, Outcome.NOT_INTERESTED)
    assert _sent(deal) == ["Email 1."]
