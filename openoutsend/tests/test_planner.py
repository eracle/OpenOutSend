"""The questions a send pass asks: who is waiting, who wrote back, and what opens a deal."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.utils import timezone

from openoutsend.core.business_time import add_business_days, business_days_between
from openoutsend.core.sequences import Sequence, Step
from openoutsend.emails.models import Kind, Thread
from openoutsend.leads.models import DealState, Outcome
from openoutsend.planner import emailable_deals, opening_step, unanswered_replies
from openoutsend.tests.emails import maillog
from openoutsend.tests.factories import DealFactory, LeadFactory

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


# ── A deal's opening step ─────────────────────────────────────────
#
# Fixed dates, because the rule is in business days: 2026-10-05 is a Monday.


def _three_steps() -> Sequence:
    return Sequence(name="s", steps=tuple(
        Step(number=n, days=days, name=f"e{n}", path=Path(f"E{n}-e{n}.prompt.j2"), source="")
        for n, days in enumerate((2, 3, 5))
    ))


def _enrolled(start: str, arrived: str = "2026-10-05T09:00:00+00:00", zone: str = ""):
    return SimpleNamespace(start_at=datetime.fromisoformat(start),
                           created_at=datetime.fromisoformat(arrived),
                           lead=SimpleNamespace(timezone=zone))


def test_e0_is_due_its_days_after_start_at():
    step, due = opening_step(_enrolled("2026-10-05T09:00:00+00:00"), _three_steps())

    assert step.number == 0
    assert due == datetime.fromisoformat("2026-10-07T00:00:00+00:00")


def test_an_e0_past_its_grace_on_arrival_is_skipped_and_e1_counts_from_its_due_time():
    """E0 was due Wednesday 30 September, three business days before the lead arrived."""
    step, due = opening_step(_enrolled("2026-09-28T09:00:00+00:00"), _three_steps())

    assert step.number == 1
    assert due == datetime.fromisoformat("2026-10-05T00:00:00+00:00")


def test_a_lead_arriving_after_every_step_was_due_opens_with_nothing():
    assert opening_step(_enrolled("2026-08-03T09:00:00+00:00"), _three_steps()) is None


def test_waiting_in_our_own_queue_never_skips_a_step():
    """Lateness is measured against the arrival, not today: the queue is ours."""
    deal = _enrolled("2026-08-03T09:00:00+00:00", arrived="2026-08-03T09:00:00+00:00")

    step, _ = opening_step(deal, _three_steps())

    assert step.number == 0


# ── One queue, oldest due first ───────────────────────────────────
#
# Friday 2026-10-09 at noon; cold's opener is due the moment the lead arrives, and a
# follow-up three business days after the send it follows.

FRIDAY_NOON = datetime.fromisoformat("2026-10-09T12:00:00+00:00")


def _opener_waiting_since(when: str):
    moment = datetime.fromisoformat(when)
    return DealFactory(lead=LeadFactory(email="new@acme.com"), start_at=moment, created_at=moment)


def _quiet_threads(box, count: int, written: str):
    """*count* cold threads whose opener went out at *written* and drew no answer."""
    for n in range(count):
        thread = Thread.objects.create(mailbox=box)
        DealFactory(lead=LeadFactory(email=f"quiet{n}@acme.com"), state=DealState.EMAILED,
                    mailbox=box, thread=thread, email_subject="Hi", step=0)
        maillog.outbound(box, thread=thread, to=f"quiet{n}@acme.com", step=0,
                         sent_at=datetime.fromisoformat(written))


def _queue(now, reached=None):
    from unittest.mock import patch

    from openoutsend.planner import cold_sends_due

    with patch("openoutsend.core.sending_window.within_sending_window", return_value=True):
        return [send.kind for send, _ in cold_sends_due(now, reached=reached)]


def test_a_backlog_of_later_sends_cannot_hold_a_waiting_opener_back(site_config):
    """Twenty threads fall due Thursday; the opener has waited since Monday, so it goes first.

    However many threads fall due after it, a first send's due time does not move —
    which is what makes the 102-follow-ups-to-1-opener week unreachable.
    """
    box = maillog.mailbox(daily_limit=50)
    _quiet_threads(box, 20, written="2026-10-05T08:00:00+00:00")
    _opener_waiting_since("2026-10-05T09:00:00+00:00")

    kinds = _queue(FRIDAY_NOON)

    assert kinds[0] == "opener"
    assert kinds.count("follow_up") == 20


def test_sends_due_before_the_opener_go_before_it(site_config):
    """Oldest due first both ways: a thread due last week is not overtaken by a new lead."""
    box = maillog.mailbox(daily_limit=50)
    _quiet_threads(box, 3, written="2026-09-28T08:00:00+00:00")
    _opener_waiting_since("2026-10-05T09:00:00+00:00")

    assert _queue(FRIDAY_NOON) == ["follow_up", "follow_up", "follow_up", "opener"]


def test_an_opener_not_yet_due_is_not_in_the_queue(site_config):
    box = maillog.mailbox(daily_limit=50)
    _quiet_threads(box, 1, written="2026-10-05T08:00:00+00:00")
    DealFactory(lead=LeadFactory(email="later@acme.com"),
                start_at=FRIDAY_NOON + timedelta(days=3), created_at=FRIDAY_NOON)

    assert _queue(FRIDAY_NOON) == ["follow_up"]


def _window_at(moment):
    """The real window, asked at *moment* — each lead's zone still decides."""
    from unittest.mock import patch

    from openoutsend.core import sending_window

    real = sending_window.within_sending_window
    return patch("openoutsend.core.sending_window.within_sending_window",
                 side_effect=lambda now=None, zone=None: real(moment, zone))


def test_a_pass_sends_to_the_leads_whose_day_it_is(site_config):
    """Friday 12:00 UTC is 14:00 in Rome and 21:00 in Tokyo: only Rome's lead is written to."""
    from openoutsend.planner import cold_sends_due

    maillog.mailbox(daily_limit=50)
    for email, zone in (("rome@acme.com", "Europe/Rome"), ("tokyo@acme.com", "Asia/Tokyo")):
        DealFactory(lead=LeadFactory(email=email, timezone=zone),
                    start_at=FRIDAY_NOON, created_at=FRIDAY_NOON)

    with _window_at(FRIDAY_NOON):
        sent = [send.deal.lead.email for send, _ in cold_sends_due(FRIDAY_NOON)]

    assert sent == ["rome@acme.com"]


def test_a_closed_window_for_one_zone_does_not_close_the_pool_for_another(site_config):
    """The older opener is Tokyo's and is skipped; the newer Rome one still finds a box."""
    from openoutsend.planner import cold_sends_due

    maillog.mailbox(daily_limit=50)
    DealFactory(lead=LeadFactory(email="tokyo@acme.com", timezone="Asia/Tokyo"),
                start_at=FRIDAY_NOON - timedelta(hours=2), created_at=FRIDAY_NOON - timedelta(hours=2))
    DealFactory(lead=LeadFactory(email="rome@acme.com", timezone="Europe/Rome"),
                start_at=FRIDAY_NOON, created_at=FRIDAY_NOON)

    with _window_at(FRIDAY_NOON):
        sent = [send.deal.lead.email for send, _ in cold_sends_due(FRIDAY_NOON)]

    assert sent == ["rome@acme.com"]


def test_the_wait_is_for_the_earliest_morning_among_the_waiting_leads(site_config):
    """Only a Tokyo lead waits on Friday 21:00 there: the next opening is Monday 08:00 Tokyo."""
    from openoutsend.planner import next_first_email_at

    maillog.mailbox(daily_limit=50)
    DealFactory(lead=LeadFactory(email="tokyo@acme.com", timezone="Asia/Tokyo"),
                start_at=FRIDAY_NOON, created_at=FRIDAY_NOON)

    assert next_first_email_at(FRIDAY_NOON) == datetime.fromisoformat("2026-10-11T23:00:00+00:00")

    DealFactory(lead=LeadFactory(email="rome@acme.com", timezone="Europe/Rome"),
                start_at=FRIDAY_NOON, created_at=FRIDAY_NOON)

    assert next_first_email_at(FRIDAY_NOON) == FRIDAY_NOON


def test_a_lead_already_written_to_this_pass_is_left_for_the_next(site_config):
    """A reply answered earlier in the pass counts: one message per address per pass."""
    box = maillog.mailbox(daily_limit=50)
    _quiet_threads(box, 1, written="2026-10-05T08:00:00+00:00")
    opener = _opener_waiting_since("2026-10-05T09:00:00+00:00")
    reached = {opener.lead_id}

    assert _queue(FRIDAY_NOON, reached) == ["follow_up"]
    assert len(reached) == 2


# ── Business days forward ─────────────────────────────────────────


def test_add_business_days_steps_over_the_weekend():
    friday = datetime.fromisoformat("2026-10-09T15:00:00+00:00")

    assert add_business_days(friday, 1) == datetime.fromisoformat("2026-10-12T00:00:00+00:00")
    assert business_days_between(friday, add_business_days(friday, 3)) == 3


def test_add_business_days_of_zero_is_the_start_itself():
    friday = datetime.fromisoformat("2026-10-09T15:00:00+00:00")

    assert add_business_days(friday, 0) == friday


def test_with_the_weekend_pause_off_every_day_counts(settings):
    """An operator who sends at weekends waits in calendar days: Friday + 1 is Saturday."""
    settings.OUTSEND_ENFORCE_WEEKEND_PAUSE = False
    friday = datetime.fromisoformat("2026-10-09T15:00:00+00:00")
    monday = datetime.fromisoformat("2026-10-12T09:00:00+00:00")

    assert add_business_days(friday, 1) == datetime.fromisoformat("2026-10-10T00:00:00+00:00")
    assert add_business_days(friday, 3) == datetime.fromisoformat("2026-10-12T00:00:00+00:00")
    assert business_days_between(friday, monday) == 3
    assert business_days_between(friday, friday + timedelta(days=15)) == 15


def test_working_days_are_counted_on_the_leads_calendar(settings):
    """Friday 22:00 UTC is already Saturday 09:00 in Sydney, so the next working day
    there is Monday in Sydney — Sunday 13:00 UTC — and Thursday evening UTC to Friday
    morning UTC is no working day at all in Sydney, where both are Friday."""
    from zoneinfo import ZoneInfo

    settings.OUTSEND_ENFORCE_WEEKEND_PAUSE = True
    sydney = ZoneInfo("Australia/Sydney")
    friday_night = datetime.fromisoformat("2026-10-09T22:00:00+00:00")

    assert add_business_days(friday_night, 1, sydney) == datetime.fromisoformat("2026-10-11T13:00:00+00:00")
    assert add_business_days(friday_night, 1) == datetime.fromisoformat("2026-10-12T00:00:00+00:00")

    thursday_evening = datetime.fromisoformat("2026-10-08T20:00:00+00:00")
    friday_morning = datetime.fromisoformat("2026-10-09T10:00:00+00:00")
    assert business_days_between(thursday_evening, friday_morning, sydney) == 0
    assert business_days_between(thursday_evening, friday_morning) == 1


def test_a_sydney_leads_opener_skips_the_sydney_weekend(settings):
    """Enrolled Friday 22:00 UTC (Saturday in Sydney) with E0 two working days out:
    Tuesday 00:00 in Sydney, not Tuesday 00:00 UTC."""
    settings.OUTSEND_ENFORCE_WEEKEND_PAUSE = True
    deal = _enrolled("2026-10-09T22:00:00+00:00", arrived="2026-10-09T22:00:00+00:00",
                     zone="Australia/Sydney")

    _, due = opening_step(deal, _three_steps())

    assert due == datetime.fromisoformat("2026-10-12T13:00:00+00:00")


def test_with_the_weekend_pause_on_friday_to_monday_is_one_day(settings):
    settings.OUTSEND_ENFORCE_WEEKEND_PAUSE = True
    friday = datetime.fromisoformat("2026-10-09T15:00:00+00:00")

    assert business_days_between(friday, datetime.fromisoformat("2026-10-12T09:00:00+00:00")) == 1
    assert business_days_between(friday, friday + timedelta(days=15)) == 10
