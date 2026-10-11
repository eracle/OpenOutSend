"""The built-in cold sequence, end to end: an opener, two follow-ups, then the end.

Every piece is tested beside its own code; this is the whole of it through the pipe and
the pass, as an operator runs it — one lead in through ingest, `send` on later days —
with only the model and SMTP stubbed.
"""
import io
import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.db.models import F

from openoutsend.core.agents.outreach import OutreachDecision
from openoutsend.emails.models import Direction, Mailbox, Message
from openoutsend.emails.steps.follow_up import FOLLOW_UP_PROMPT
from openoutsend.leads.ingest import ingest
from openoutsend.leads.models import Deal, DealState
from openoutsend.send_pass import run_send_pass
from openoutsend.tests.emails import maillog

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
        return OutreachDecision(action="send_message", message=f"Email {len(prompts)}.",
                                subject="A question" if opener else None)

    with patch("openoutsend.emails.mail_pass.run_mail_pass", return_value=(0, 0, 0)), \
            patch("openoutsend.core.sending_window.within_sending_window", return_value=True), \
            patch("openoutsend.leads.summaries.materialize_profile_summary_if_missing"), \
            patch("openoutsend.core.agents.outreach.run_outreach_agent", side_effect=write), \
            patch("openoutsend.emails.sender._deliver"):
        ingest(io.StringIO(json.dumps(LEAD) + "\n"))
        yield prompts


def _two_weeks_pass():
    """Age the mail log, so the next pass runs as if ten working days had gone by."""
    Message.objects.update(sent_at=F("sent_at") - timedelta(days=14))
    Mailbox.objects.update(next_send_at=None)


def _deal() -> Deal:
    return Deal.objects.get(lead__email=LEAD["email"])


def test_a_silent_lead_gets_the_opener_two_follow_ups_and_then_nothing(written):
    run_send_pass()
    deal = _deal()
    assert (deal.sequence, deal.step, deal.state) == ("cold", 0, DealState.EMAILED)

    for _ in range(2):
        _two_weeks_pass()
        run_send_pass()

    assert written[1:] == [FOLLOW_UP_PROMPT, FOLLOW_UP_PROMPT]
    assert deal.thread.messages.filter(direction=Direction.OUTBOUND).count() == 3

    _two_weeks_pass()
    run_send_pass()

    deal = _deal()
    assert (deal.state, deal.outcome) == (DealState.COMPLETED, "unresponsive")
    assert len(written) == 3


def test_a_pass_on_the_same_day_does_not_chase(written):
    run_send_pass()
    run_send_pass()

    assert len(written) == 1


def test_with_follow_ups_off_the_opener_is_the_whole_pursuit(written, monkeypatch):
    monkeypatch.setenv("OUTSEND_FOLLOW_UPS", "off")
    run_send_pass()
    _two_weeks_pass()
    run_send_pass()

    deal = _deal()
    assert (deal.state, deal.outcome) == (DealState.COMPLETED, "unresponsive")
    assert len(written) == 1
