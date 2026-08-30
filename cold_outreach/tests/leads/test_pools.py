"""The two questions a send pass asks: who is waiting, and who wrote back."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from cold_outreach.emails.models import Kind, Thread
from cold_outreach.leads.models import DealState, Outcome
from cold_outreach.leads.pools import emailable_deals, unanswered_replies
from cold_outreach.tests.emails import maillog
from cold_outreach.tests.factories import DealFactory, LeadFactory

pytestmark = pytest.mark.django_db


def _ready(*, email="lead@acme.com", **kwargs):
    return DealFactory(lead=LeadFactory(email=email), **kwargs)


# ── Waiting for a first email ─────────────────────────────────────


def test_emailable_deals_returns_ready_rows_oldest_first():
    older = _ready(created_at=timezone.now() - timedelta(days=7))
    newer = _ready(email="other@acme.com")

    assert list(emailable_deals()) == [older, newer]


def test_emailable_deals_skips_rows_with_no_address():
    """A row with no address rests in READY; it is an enrichment away, not sendable."""
    _ready(email="")

    assert list(emailable_deals()) == []


@pytest.mark.parametrize("state", [DealState.EMAILED, DealState.COMPLETED])
def test_emailable_deals_skips_every_state_but_ready(state):
    """Suppression needs no filter of its own — an opt-out parks the row instead."""
    _ready(state=state)

    assert list(emailable_deals()) == []


# ── Waiting for an answer ─────────────────────────────────────────


def _emailed(box, **kwargs):
    """An EMAILED deal pointing at a thread its opener already started."""
    thread = Thread.objects.create(mailbox=box)
    deal = DealFactory(
        lead=LeadFactory(email="lead@acme.com"),
        state=DealState.EMAILED, mailbox=box, thread=thread, **kwargs,
    )
    maillog.outbound(box, thread=thread, sent_at=timezone.now() - timedelta(hours=2))
    return deal


def test_unanswered_replies_finds_a_thread_whose_newest_turn_is_theirs():
    box = maillog.mailbox()
    deal = _emailed(box)
    maillog.inbound(box, thread=deal.thread)

    assert list(unanswered_replies()) == [deal]


def test_unanswered_replies_drops_a_thread_we_already_answered():
    box = maillog.mailbox()
    deal = _emailed(box)
    maillog.inbound(box, thread=deal.thread, sent_at=timezone.now() - timedelta(hours=1))
    maillog.outbound(box, thread=deal.thread)

    assert list(unanswered_replies()) == []


def test_unanswered_replies_ignores_silence():
    _emailed(maillog.mailbox())

    assert list(unanswered_replies()) == []


@pytest.mark.parametrize("kind", [Kind.BOUNCE, Kind.AUTO_REPLY, Kind.UNRELATED])
def test_unanswered_replies_ignores_what_nobody_said(kind):
    """A bounce sits in the thread and is not somebody writing back."""
    box = maillog.mailbox()
    deal = _emailed(box)
    maillog.inbound(box, thread=deal.thread, kind=kind)

    assert list(unanswered_replies()) == []


def test_unanswered_replies_drops_a_finished_conversation():
    box = maillog.mailbox()
    deal = _emailed(box, outcome=Outcome.NOT_INTERESTED)
    maillog.inbound(box, thread=deal.thread)

    assert list(unanswered_replies()) == []


def test_unanswered_replies_answers_the_oldest_reply_first():
    box = maillog.mailbox()
    waited = _emailed(box)
    fresh = _emailed(box)
    maillog.inbound(box, thread=waited.thread, sent_at=timezone.now() - timedelta(hours=1))
    maillog.inbound(box, thread=fresh.thread)

    assert list(unanswered_replies()) == [waited, fresh]
