"""The two questions a send pass asks: who is waiting, and who wrote back."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from cold_outreach.emails.models import Kind, Thread
from cold_outreach.leads.models import DealState, Outcome
from cold_outreach.leads.pools import emailable_deals, unanswered_replies
from cold_outreach.tests.emails import maillog
from cold_outreach.tests.factories import CampaignFactory, DealFactory, LeadFactory

pytestmark = pytest.mark.django_db


def _ready(campaign, *, email="lead@acme.com", **kwargs):
    return DealFactory(campaign=campaign, lead=LeadFactory(email=email), **kwargs)


# ── Waiting for a first email ─────────────────────────────────────


def test_emailable_deals_returns_ready_rows_oldest_first(campaign):
    older = _ready(campaign, created_at=timezone.now() - timedelta(days=7))
    newer = _ready(campaign, email="other@acme.com")

    assert list(emailable_deals(campaign)) == [older, newer]


def test_emailable_deals_skips_rows_with_no_address(campaign):
    """A row with no address rests in READY; it is an enrichment away, not sendable."""
    _ready(campaign, email="")

    assert list(emailable_deals(campaign)) == []


@pytest.mark.parametrize("state", [DealState.EMAILED, DealState.COMPLETED, DealState.UNSUBSCRIBED])
def test_emailable_deals_skips_every_state_but_ready(campaign, state):
    """Suppression needs no filter of its own — an opt-out parks the row instead."""
    _ready(campaign, state=state)

    assert list(emailable_deals(campaign)) == []


def test_emailable_deals_is_scoped_to_its_campaign(campaign):
    _ready(CampaignFactory())
    mine = _ready(campaign)

    assert list(emailable_deals(campaign)) == [mine]


# ── Waiting for an answer ─────────────────────────────────────────


def _emailed(campaign, box, **kwargs):
    """An EMAILED deal pointing at a thread its opener already started."""
    thread = Thread.objects.create(mailbox=box)
    deal = DealFactory(
        campaign=campaign, lead=LeadFactory(email="lead@acme.com"),
        state=DealState.EMAILED, mailbox=box, thread=thread, **kwargs,
    )
    maillog.outbound(box, thread=thread, sent_at=timezone.now() - timedelta(hours=2))
    return deal


def test_unanswered_replies_finds_a_thread_whose_newest_turn_is_theirs(campaign):
    box = maillog.mailbox()
    deal = _emailed(campaign, box)
    maillog.inbound(box, thread=deal.thread)

    assert list(unanswered_replies(campaign)) == [deal]


def test_unanswered_replies_drops_a_thread_we_already_answered(campaign):
    box = maillog.mailbox()
    deal = _emailed(campaign, box)
    maillog.inbound(box, thread=deal.thread, sent_at=timezone.now() - timedelta(hours=1))
    maillog.outbound(box, thread=deal.thread)

    assert list(unanswered_replies(campaign)) == []


def test_unanswered_replies_ignores_silence(campaign):
    _emailed(campaign, maillog.mailbox())

    assert list(unanswered_replies(campaign)) == []


@pytest.mark.parametrize("kind", [Kind.BOUNCE, Kind.AUTO_REPLY, Kind.UNRELATED])
def test_unanswered_replies_ignores_what_nobody_said(campaign, kind):
    """A bounce sits in the thread and is not somebody writing back."""
    box = maillog.mailbox()
    deal = _emailed(campaign, box)
    maillog.inbound(box, thread=deal.thread, kind=kind)

    assert list(unanswered_replies(campaign)) == []


def test_unanswered_replies_drops_a_finished_conversation(campaign):
    box = maillog.mailbox()
    deal = _emailed(campaign, box, outcome=Outcome.NOT_INTERESTED)
    maillog.inbound(box, thread=deal.thread)

    assert list(unanswered_replies(campaign)) == []


def test_unanswered_replies_answers_the_oldest_reply_first(campaign):
    box = maillog.mailbox()
    waited = _emailed(campaign, box)
    fresh = _emailed(campaign, box)
    maillog.inbound(box, thread=waited.thread, sent_at=timezone.now() - timedelta(hours=1))
    maillog.inbound(box, thread=fresh.thread)

    assert list(unanswered_replies(campaign)) == [waited, fresh]
