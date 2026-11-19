"""The history a message is written from: every turn with the lead, verbatim, in order,
as the model's message history: our emails its own turns, theirs and the silences the user's."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from pydantic_ai.messages import ModelRequest, ModelResponse

from openoutsend.core.agents.history import OPENING, history, transcript
from openoutsend.emails.models import Kind, Message, Thread
from openoutsend.tests.emails import maillog
from openoutsend.tests.factories import DealFactory, LeadFactory

pytestmark = pytest.mark.django_db


def _monday(hour=10):
    """A fixed Monday, so business-day gaps are exact."""
    return timezone.now().replace(
        year=2026, month=3, day=16, hour=hour, minute=0, second=0, microsecond=0)


def _said(messages) -> list[tuple[str, list[str]]]:
    """Each message as (who, the text of each of its parts)."""
    return [
        ("us" if isinstance(message, ModelResponse) else "them", [part.content for part in message.parts])
        for message in messages
    ]


@pytest.fixture
def box():
    return maillog.mailbox()


@pytest.fixture
def deal(box):
    return DealFactory(lead=LeadFactory(email="lead@acme.com"),
                       thread=Thread.objects.create(mailbox=box))


def test_nothing_yet_is_an_empty_history(deal):
    assert history(deal.lead, _monday()) == []


def test_our_email_is_our_turn_and_the_prompt_it_was_written_from_is_never_shown(box, deal):
    maillog.outbound(box, thread=deal.thread, body="Hello.", subject="A question",
                     prompt="Say hello, in German, and name the price.", step=0, sent_at=_monday())

    messages = history(deal.lead, _monday())

    assert _said(messages) == [
        ("them", [OPENING]),
        ("us", ['[2026-03-16, subject "A question"]\nHello.']),
        ("them", ["[no reply for 0 business days]"]),
    ]
    assert "price" not in repr(messages)


def test_their_reply_is_their_turn_and_there_is_no_silence_before_it(box, deal):
    maillog.outbound(box, thread=deal.thread, body="Hello.", sent_at=_monday())
    maillog.inbound(box, thread=deal.thread, body="Who is this?",
                    sent_at=_monday() + timedelta(days=1))

    said = _said(history(deal.lead, _monday() + timedelta(days=1)))

    assert said[-1] == ("them", ["[2026-03-17]\nWho is this?"])
    assert "no reply" not in repr(said)


def test_silence_between_two_of_our_sends_is_their_turn_in_business_days(box, deal):
    maillog.outbound(box, thread=deal.thread, body="Hello.", sent_at=_monday())
    maillog.outbound(box, thread=deal.thread, body="Again.", sent_at=_monday() + timedelta(days=3))

    said = _said(history(deal.lead, _monday() + timedelta(days=3)))

    assert [who for who, _ in said] == ["them", "us", "them", "us", "them"]
    assert said[2] == ("them", ["[no reply for 3 business days]"])


def test_silence_after_our_last_send_runs_to_now(box, deal):
    """A follow-up is written into silence, and the history says how long it has been."""
    maillog.outbound(box, thread=deal.thread, body="Hello.", sent_at=_monday())

    messages = history(deal.lead, _monday() + timedelta(days=7))

    assert isinstance(messages[-1], ModelRequest)
    assert _said(messages)[-1] == ("them", ["[no reply for 5 business days]"])


def test_two_replies_in_a_row_are_one_turn_of_theirs(box, deal):
    maillog.outbound(box, thread=deal.thread, body="Hello.", sent_at=_monday())
    maillog.inbound(box, thread=deal.thread, body="Who?", sent_at=_monday() + timedelta(hours=1))
    maillog.inbound(box, thread=deal.thread, body="Hello?", sent_at=_monday() + timedelta(hours=2))

    said = _said(history(deal.lead, _monday() + timedelta(hours=2)))

    assert said[-1] == ("them", ["[2026-03-16]\nWho?", "[2026-03-16]\nHello?"])


def test_a_bounce_is_not_a_turn(box, deal):
    maillog.outbound(box, thread=deal.thread, body="Hello.", sent_at=_monday())
    maillog.inbound(box, thread=deal.thread, body="Delivery failed", kind=Kind.BOUNCE)

    assert "Delivery failed" not in repr(history(deal.lead, _monday()))


def test_it_reads_only_the_threads_of_this_leads_deals(box, deal):
    other = DealFactory(lead=LeadFactory(lead_id="other", email="someone@else.com"),
                        thread=Thread.objects.create(mailbox=box))
    maillog.outbound(box, thread=deal.thread, body="Theirs.", sent_at=_monday())
    maillog.outbound(box, thread=other.thread, body="Somebody else's.", sent_at=_monday())
    maillog.outbound(box, thread=Thread.objects.create(mailbox=box), body="Nobody's.",
                     sent_at=_monday())

    text = repr(history(deal.lead, _monday()))

    assert "Theirs." in text
    assert "Somebody else's." not in text
    assert "Nobody's." not in text


def test_the_transcript_says_the_same_as_text_and_no_prompt_either(box, deal):
    maillog.outbound(box, thread=deal.thread, body="Hello.", subject="A question",
                     prompt="Say hello.", step=0, sent_at=_monday())
    maillog.inbound(box, thread=deal.thread, body="Who is this?", sent_at=_monday() + timedelta(days=1))
    turns = list(Message.objects.filter(thread=deal.thread).order_by("sent_at", "pk"))

    text = transcript(turns, _monday() + timedelta(days=1))

    assert text == (
        'YOU SENT:\n[2026-03-16, subject "A question"]\nHello.\n\n'
        "THEY WROTE:\n[2026-03-17]\nWho is this?"
    )
