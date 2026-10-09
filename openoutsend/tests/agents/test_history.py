"""The history a message is written from: every turn with the lead, verbatim, in order."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from openoutsend.core.agents.history import history
from openoutsend.emails.models import Kind, Thread
from openoutsend.tests.emails import maillog
from openoutsend.tests.factories import DealFactory, LeadFactory

pytestmark = pytest.mark.django_db


def _monday(hour=10):
    """A fixed Monday, so business-day gaps are exact."""
    return timezone.now().replace(
        year=2026, month=3, day=16, hour=hour, minute=0, second=0, microsecond=0)


@pytest.fixture
def box():
    return maillog.mailbox()


@pytest.fixture
def deal(box):
    return DealFactory(lead=LeadFactory(email="lead@acme.com"),
                       thread=Thread.objects.create(mailbox=box))


def test_nothing_yet_is_an_empty_history(deal):
    assert history(deal.lead, _monday()) == ""


def test_our_send_carries_the_prompt_it_was_written_from(box, deal):
    maillog.outbound(box, thread=deal.thread, body="Hello.", subject="A question",
                     prompt="Say hello.", step=0, sent_at=_monday())

    text = history(deal.lead, _monday())

    assert text.index("PROMPT YOU WERE GIVEN:\nSay hello.") < text.index('YOU SENT (subject "A question"):\nHello.')


def test_their_reply_follows_and_there_is_no_silence_before_it(box, deal):
    maillog.outbound(box, thread=deal.thread, body="Hello.", sent_at=_monday())
    maillog.inbound(box, thread=deal.thread, body="Who is this?",
                    sent_at=_monday() + timedelta(days=1))

    text = history(deal.lead, _monday() + timedelta(days=1))

    assert "THEY WROTE:\nWho is this?" in text
    assert "NO REPLY" not in text


def test_silence_between_two_of_our_sends_is_stated_in_business_days(box, deal):
    maillog.outbound(box, thread=deal.thread, body="Hello.", sent_at=_monday())
    maillog.outbound(box, thread=deal.thread, body="Again.", sent_at=_monday() + timedelta(days=3))

    text = history(deal.lead, _monday() + timedelta(days=3))

    assert text.index("Hello.") < text.index("[NO REPLY for 3 business days]") < text.index("Again.")


def test_silence_after_our_last_send_runs_to_now(box, deal):
    """A follow-up is written into silence, and the history says how long it has been."""
    maillog.outbound(box, thread=deal.thread, body="Hello.", sent_at=_monday())

    text = history(deal.lead, _monday() + timedelta(days=7))

    assert text.endswith("[NO REPLY for 5 business days]")


def test_a_bounce_is_not_a_turn(box, deal):
    maillog.outbound(box, thread=deal.thread, body="Hello.", sent_at=_monday())
    maillog.inbound(box, thread=deal.thread, body="Delivery failed", kind=Kind.BOUNCE)

    assert "Delivery failed" not in history(deal.lead, _monday())


def test_it_reads_only_the_threads_of_this_leads_deals(box, deal):
    other = DealFactory(lead=LeadFactory(lead_id="other", email="someone@else.com"),
                        thread=Thread.objects.create(mailbox=box))
    maillog.outbound(box, thread=deal.thread, body="Theirs.", sent_at=_monday())
    maillog.outbound(box, thread=other.thread, body="Somebody else's.", sent_at=_monday())
    maillog.outbound(box, thread=Thread.objects.create(mailbox=box), body="Nobody's.",
                     sent_at=_monday())

    text = history(deal.lead, _monday())

    assert "Theirs." in text
    assert "Somebody else's." not in text
    assert "Nobody's." not in text
