"""The questions a send pass asks: whose step is due, who wrote back, and which box may send."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from openoutsend.core.sending_window import operator_timezone
from openoutsend.emails.models import Kind, Thread
from openoutsend.leads.models import Outcome
from openoutsend.leads.states import COMPLETED, CONVERSING, awaiting, machine_name, sending
from openoutsend.planner import (
    conversations_waiting,
    due_deals,
    free_box,
    free_for_first_email,
    free_thread_box,
    next_first_email_at,
    sends_due,
    waiting_to_open,
)
from openoutsend.tests.emails import maillog
from openoutsend.tests.factories import DealFactory, LeadFactory, put_in_state, sequence_folder

pytestmark = pytest.mark.django_db


def _due(*, email="lead@acme.com", since=None, **kwargs):
    """A deal whose opener is due: resting in `sending_E0`, since *since* (now when left out)."""
    deal = DealFactory(lead=LeadFactory(email=email), **kwargs)
    put_in_state(deal, sending(0), at=since)
    return deal


def _in_thread(box, sequence, state, *, email="lead@acme.com", since=None, **kwargs):
    """A deal of *sequence* already written to from *box*, resting in *state* since *since*."""
    deal = DealFactory(lead=LeadFactory(email=email), sequence=sequence, mailbox=box,
                       email_subject="Hi", **kwargs)
    deal.thread = Thread.objects.create(mailbox=box, machine=machine_name(sequence),
                                        entity=str(deal.pk))
    deal.save()
    put_in_state(deal, state, at=since)
    maillog.outbound(box, thread=deal.thread, to=email, step=0,
                     sent_at=timezone.now() - timedelta(days=7))
    return deal


def _open_window():
    return patch("openoutsend.core.sending_window.within_sending_window", return_value=True)


# ── Whose step is due ─────────────────────────────────────────────


def test_due_deals_returns_the_deals_in_a_sending_state_longest_due_first():
    newer = _due(email="other@acme.com")
    older = _due(since=timezone.now() - timedelta(days=7))

    due = due_deals()

    assert due == [older, newer]
    assert [deal.due_step.number for deal in due] == [0, 0]


def test_a_due_deal_carries_the_step_its_state_names(two_steps):
    deal = _in_thread(maillog.mailbox(), two_steps, sending(1))

    (due,) = due_deals()

    assert due == deal
    assert due.due_step.path.name == "E1-3d-steer.prompt.j2"


def test_due_deals_skips_rows_with_no_address():
    """A row with no address rests in its `sending` state; it is an enrichment away, not sendable."""
    _due(email="")

    assert due_deals() == []


@pytest.mark.parametrize("state", [awaiting(0), CONVERSING, COMPLETED])
def test_due_deals_skips_every_state_but_a_sending_one(state):
    """Suppression needs no filter of its own — an opt-out ends the deal instead."""
    DealFactory(lead=LeadFactory(email="lead@acme.com"), state=state)

    assert due_deals() == []


def test_due_deals_narrows_to_one_sequence(two_steps):
    cold = _due()
    _in_thread(maillog.mailbox(), two_steps, sending(1), email="other@acme.com")

    assert due_deals("cold") == [cold]


def test_waiting_to_open_is_every_open_deal_not_yet_written_to(two_steps):
    """Who a first email is still owed, due or not — what a bounded run asks before it waits."""
    not_yet_due = DealFactory(lead=LeadFactory(email="later@acme.com"))
    due = _due()
    _due(email="")
    DealFactory(lead=LeadFactory(email="done@acme.com"), state=COMPLETED)
    _in_thread(maillog.mailbox(), two_steps, sending(1), email="written@acme.com")

    assert set(waiting_to_open()) == {not_yet_due, due}


# ── Waiting for an answer ─────────────────────────────────────────


def _conversing(box, state=CONVERSING, **kwargs):
    """A deal past its last step, resting in its conversation, its opener sent two hours ago."""
    deal = DealFactory(lead=LeadFactory(email="lead@acme.com"), state=state, mailbox=box,
                       **kwargs)
    deal.thread = Thread.objects.create(mailbox=box, machine=machine_name(deal.sequence),
                                        entity=str(deal.pk))
    deal.save()
    maillog.outbound(box, thread=deal.thread, sent_at=timezone.now() - timedelta(hours=2))
    return deal


def test_conversations_waiting_finds_a_thread_whose_newest_turn_is_theirs():
    box = maillog.mailbox()
    deal = _conversing(box)
    maillog.inbound(box, thread=deal.thread)

    assert conversations_waiting() == [deal]


def test_conversations_waiting_drops_a_thread_we_already_answered():
    box = maillog.mailbox()
    deal = _conversing(box)
    maillog.inbound(box, thread=deal.thread, sent_at=timezone.now() - timedelta(hours=1))
    maillog.outbound(box, thread=deal.thread)

    assert conversations_waiting() == []


def test_conversations_waiting_ignores_silence():
    _conversing(maillog.mailbox())

    assert conversations_waiting() == []


@pytest.mark.parametrize("kind", [Kind.BOUNCE, Kind.AUTO_REPLY, Kind.UNRELATED])
def test_conversations_waiting_ignores_what_nobody_said(kind):
    """A bounce sits in the thread and is not somebody writing back."""
    box = maillog.mailbox()
    deal = _conversing(box)
    maillog.inbound(box, thread=deal.thread, kind=kind)

    assert conversations_waiting() == []


def test_conversations_waiting_drops_a_finished_conversation():
    box = maillog.mailbox()
    deal = _conversing(box, state=COMPLETED, outcome=Outcome.NOT_INTERESTED)
    maillog.inbound(box, thread=deal.thread)

    assert conversations_waiting() == []


def test_a_reply_during_the_steps_waits_until_the_deal_is_conversing(two_steps):
    """A reply never stops a step, and is answered only once the last one has gone."""
    box = maillog.mailbox()
    deal = _in_thread(box, two_steps, awaiting(1))
    maillog.inbound(box, thread=deal.thread)

    assert conversations_waiting() == []

    put_in_state(deal, CONVERSING)

    assert conversations_waiting() == [deal]


def test_nobody_suppressed_is_owed_an_answer():
    from openoutsend.leads.models import Suppression

    box = maillog.mailbox()
    deal = _conversing(box)
    maillog.inbound(box, thread=deal.thread)
    Suppression.objects.create(email="lead@acme.com")

    assert conversations_waiting() == []


def test_conversations_waiting_answers_the_oldest_reply_first():
    box = maillog.mailbox()
    waited = _conversing(box)
    fresh = _conversing(box)
    maillog.inbound(box, thread=waited.thread, sent_at=timezone.now() - timedelta(hours=1))
    maillog.inbound(box, thread=fresh.thread)

    assert conversations_waiting() == [waited, fresh]


# ── One queue, longest due first ──────────────────────────────────
#
# Friday 2026-10-09 at noon. A deal is due from the moment it came to rest in its
# `sending` state, whichever step that is.

FRIDAY_NOON = datetime.fromisoformat("2026-10-09T12:00:00+00:00")

OPENER, STEP = "opener", "step"


def _opener_due_since(when: str):
    return _due(email="new@acme.com", since=datetime.fromisoformat(when))


def _later_steps_due(box, sequence, count: int, since: str):
    """*count* threads of *sequence* whose `E1` fell due at *since*."""
    for n in range(count):
        _in_thread(box, sequence, sending(1), email=f"quiet{n}@acme.com",
                   since=datetime.fromisoformat(since))


def _queue(reached=None):
    """What the guards let go now, in order: a first email, or a later step in its thread."""
    with _open_window():
        return [OPENER if deal.thread_id is None else STEP
                for deal, _ in sends_due(reached=reached)]


def test_a_backlog_of_later_sends_cannot_hold_a_waiting_opener_back(site_config, two_steps):
    """Twenty threads fall due Thursday; the opener has been due since Monday, so it goes first.

    However many threads fall due after it, a first send's due time does not move —
    which is what makes the 102-later-sends-to-1-opener week unreachable.
    """
    box = maillog.mailbox(daily_limit=50)
    _later_steps_due(box, two_steps, 20, since="2026-10-08T08:00:00+00:00")
    _opener_due_since("2026-10-05T09:00:00+00:00")

    kinds = _queue()

    assert kinds[0] == OPENER
    assert kinds.count(STEP) == 20


def test_sends_due_before_the_opener_go_before_it(site_config, two_steps):
    """Longest due first both ways: a thread due last week is not overtaken by a new lead."""
    box = maillog.mailbox(daily_limit=50)
    _later_steps_due(box, two_steps, 3, since="2026-10-01T08:00:00+00:00")
    _opener_due_since("2026-10-05T09:00:00+00:00")

    assert _queue() == [STEP, STEP, STEP, OPENER]


def test_an_opener_not_yet_due_is_not_in_the_queue(site_config, two_steps):
    box = maillog.mailbox(daily_limit=50)
    _later_steps_due(box, two_steps, 1, since="2026-10-08T08:00:00+00:00")
    DealFactory(lead=LeadFactory(email="later@acme.com"))

    assert _queue() == [STEP]


def test_a_pass_narrowed_to_a_sequence_queues_only_its_deals(site_config, two_steps):
    box = maillog.mailbox(daily_limit=50)
    _later_steps_due(box, two_steps, 2, since="2026-10-08T08:00:00+00:00")
    _opener_due_since("2026-10-05T09:00:00+00:00")

    with _open_window():
        assert [deal.sequence for deal, _ in sends_due(sequence=two_steps)] == [two_steps] * 2


def test_an_agent_drafting_pass_queues_no_step_written_from_a_prompt(site_config, tmp_path):
    """A prompt step is the calling agent's to write; an email step has nothing to draft."""
    as_written = sequence_folder(tmp_path, "as-written", {"E0-hello.email.j2": "Subject: Hello\n\nHi."})
    maillog.mailbox(daily_limit=50)
    _opener_due_since("2026-10-05T09:00:00+00:00")
    written = _due(email="written@acme.com", sequence=as_written)

    with _open_window():
        assert [deal for deal, _ in sends_due(agent_drafted=True)] == [written]


def _window_at(moment):
    """The real window, asked at *moment* — each lead's zone still decides."""
    from openoutsend.core import sending_window

    real = sending_window.within_sending_window
    return patch("openoutsend.core.sending_window.within_sending_window",
                 side_effect=lambda now=None, zone=None: real(moment, zone))


def test_a_pass_sends_to_the_leads_whose_day_it_is(site_config):
    """Friday 12:00 UTC is 14:00 in Rome and 21:00 in Tokyo: only Rome's lead is written to."""
    maillog.mailbox(daily_limit=50)
    for email, zone in (("rome@acme.com", "Europe/Rome"), ("tokyo@acme.com", "Asia/Tokyo")):
        DealFactory(lead=LeadFactory(email=email, timezone=zone), state=sending(0))

    with _window_at(FRIDAY_NOON):
        sent = [deal.lead.email for deal, _ in sends_due()]

    assert sent == ["rome@acme.com"]


def test_a_closed_window_for_one_zone_does_not_close_the_pool_for_another(site_config):
    """The longer-due opener is Tokyo's and is skipped; the newer Rome one still finds a box."""
    maillog.mailbox(daily_limit=50)
    put_in_state(DealFactory(lead=LeadFactory(email="tokyo@acme.com", timezone="Asia/Tokyo")),
                 sending(0), at=FRIDAY_NOON - timedelta(hours=2))
    put_in_state(DealFactory(lead=LeadFactory(email="rome@acme.com", timezone="Europe/Rome")),
                 sending(0), at=FRIDAY_NOON)

    with _window_at(FRIDAY_NOON):
        sent = [deal.lead.email for deal, _ in sends_due()]

    assert sent == ["rome@acme.com"]


def test_a_later_step_waits_for_its_own_leads_day(site_config, two_steps):
    """A later step is cold volume too: Tokyo's thread is skipped at 21:00 there."""
    box = maillog.mailbox(daily_limit=50)
    deal = _in_thread(box, two_steps, sending(1))
    deal.lead.timezone = "Asia/Tokyo"
    deal.lead.save()

    with _window_at(FRIDAY_NOON):
        assert list(sends_due()) == []


def test_the_wait_is_for_the_earliest_morning_among_the_waiting_leads(site_config):
    """Only a Tokyo lead waits on Friday 21:00 there: the next opening is Monday 08:00 Tokyo."""
    maillog.mailbox(daily_limit=50)
    DealFactory(lead=LeadFactory(email="tokyo@acme.com", timezone="Asia/Tokyo"),
                start_at=FRIDAY_NOON, created_at=FRIDAY_NOON)

    assert next_first_email_at(FRIDAY_NOON) == datetime.fromisoformat("2026-10-11T23:00:00+00:00")

    DealFactory(lead=LeadFactory(email="rome@acme.com", timezone="Europe/Rome"),
                start_at=FRIDAY_NOON, created_at=FRIDAY_NOON)

    assert next_first_email_at(FRIDAY_NOON) == FRIDAY_NOON


def test_no_first_email_is_worth_waking_for_before_the_earliest_is_due(site_config, tmp_path):
    """A deal enrolled Friday for `E0-2d` is due Tuesday 00:00, so the wait is Tuesday 08:00."""
    in_two_days = sequence_folder(tmp_path, "in-two-days", {"E0-2d-open.prompt.j2": "Open."})
    maillog.mailbox(daily_limit=50)
    DealFactory(lead=LeadFactory(email="rome@acme.com", timezone="Europe/Rome"),
                sequence=in_two_days, start_at=FRIDAY_NOON, created_at=FRIDAY_NOON)

    assert next_first_email_at(FRIDAY_NOON) == datetime.fromisoformat("2026-10-13T06:00:00+00:00")


def test_with_no_mailbox_no_first_email_can_ever_go(site_config):
    DealFactory(lead=LeadFactory(email="rome@acme.com"), start_at=FRIDAY_NOON, created_at=FRIDAY_NOON)

    assert next_first_email_at(FRIDAY_NOON) is None


def test_a_lead_already_written_to_this_pass_is_left_for_the_next(site_config, two_steps):
    """A reply answered earlier in the pass counts: one message per address per pass."""
    box = maillog.mailbox(daily_limit=50)
    _later_steps_due(box, two_steps, 1, since="2026-10-08T08:00:00+00:00")
    opener = _opener_due_since("2026-10-05T09:00:00+00:00")
    reached = {opener.lead_id}

    assert _queue(reached) == [STEP]
    assert len(reached) == 2


def test_a_lead_due_in_two_sequences_is_queued_once(site_config, two_steps):
    """Two deals of one lead fall due together: the longer due goes, the other waits a pass."""
    box = maillog.mailbox(daily_limit=50)
    lead = LeadFactory(email="both@acme.com")
    cold = DealFactory(lead=lead)
    put_in_state(cold, sending(0), at=datetime.fromisoformat("2026-10-05T09:00:00+00:00"))
    DealFactory(lead=lead, sequence=two_steps, state=sending(0))

    with _open_window():
        assert [deal for deal, _ in sends_due()] == [cold]
    assert box.headroom_today() == 50


# ── From which box ────────────────────────────────────────────────


def test_a_first_email_takes_the_box_with_the_most_headroom(site_config):
    maillog.mailbox("busy@corp.com", daily_limit=2)
    idle = maillog.mailbox("idle@corp.com", daily_limit=9)

    with _open_window():
        assert free_for_first_email() == idle
        assert free_box(_due()) == idle


def test_a_box_out_of_headroom_opens_nothing(site_config):
    maillog.mailbox(daily_limit=0)

    with _open_window():
        assert free_for_first_email() is None


def test_a_box_inside_its_spacing_clock_opens_nothing(site_config):
    spaced = maillog.mailbox("spaced@corp.com", daily_limit=9,
                             next_send_at=timezone.now() + timedelta(minutes=3))

    with _open_window():
        assert free_for_first_email() is None

        free = maillog.mailbox("free@corp.com", daily_limit=1)
        assert free_for_first_email() == free
    assert spaced.headroom_today() == 9


def test_outside_the_window_no_box_opens_a_conversation(site_config):
    maillog.mailbox(daily_limit=9)

    with patch("openoutsend.core.sending_window.within_sending_window", return_value=False):
        assert free_for_first_email() is None


def test_a_later_step_stays_on_its_threads_box(site_config, two_steps):
    """Another box is freer, and the thread's own still carries it."""
    own = maillog.mailbox("own@corp.com", daily_limit=2)
    maillog.mailbox("idle@corp.com", daily_limit=9)
    deal = _in_thread(own, two_steps, sending(1))

    with _open_window():
        assert free_thread_box(deal) == own
        assert free_box(deal) == own


def test_a_later_step_waits_while_its_threads_box_is_spaced_or_spent(site_config, two_steps):
    """A busy box holds its own threads to the next pass, however free the rest are."""
    own = maillog.mailbox("own@corp.com", daily_limit=2)
    maillog.mailbox("idle@corp.com", daily_limit=9)
    deal = _in_thread(own, two_steps, sending(1))

    with _open_window():
        own.next_send_at = timezone.now() + timedelta(minutes=3)
        own.save()
        assert free_thread_box(deal) is None

        own.next_send_at = None
        own.daily_limit = 0
        own.save()
        deal.mailbox.refresh_from_db()
        assert free_thread_box(deal) is None
        assert list(sends_due()) == []


def test_the_spacing_clock_is_read_again_for_each_send(site_config, two_steps):
    """The queue is built before the loop that sends, so a send earlier in it moved the
    clock on another copy of the box."""
    from openoutsend.emails.models import Mailbox

    box = maillog.mailbox(daily_limit=9)
    deal = _in_thread(box, two_steps, sending(1))
    Mailbox.objects.filter(pk=box.pk).update(next_send_at=timezone.now() + timedelta(minutes=3))

    with _open_window():
        assert free_thread_box(deal) is None


# ── When the pool could next open a conversation ──────────────────
#
# The guards `free_for_first_email` enforces, read as a clock rather than as a boolean,
# which is what lets a bounded run wait for them.


def _window_held_open():
    """Ask the clock with the window held open, so one gate is tested at a time."""
    return patch("openoutsend.core.sending_window.next_window_open",
                 side_effect=lambda moment, zone=None: moment)


def test_no_mailbox_is_the_one_answer_no_clock_resolves():
    """None, not a time: waiting cannot connect a box, so a run must stop instead."""
    assert next_first_email_at() is None


def test_a_free_box_could_send_now():
    maillog.mailbox()
    now = timezone.now()

    with _window_held_open():
        assert next_first_email_at(now) == now


def test_a_spaced_box_is_free_when_its_clock_elapses():
    now = timezone.now()
    maillog.mailbox(next_send_at=now + timedelta(minutes=4))

    with _window_held_open():
        assert next_first_email_at(now) == now + timedelta(minutes=4)


def test_an_elapsed_clock_does_not_answer_in_the_past():
    """A `next_send_at` from yesterday means *now*, not a moment already gone."""
    now = timezone.now()
    maillog.mailbox(next_send_at=now - timedelta(hours=3))

    with _window_held_open():
        assert next_first_email_at(now) == now


def test_a_free_box_waits_for_the_earliest_opener_to_fall_due():
    """A lead enrolled for later is not worth waking for now, however free the box."""
    now = timezone.now()
    maillog.mailbox()
    DealFactory(lead=LeadFactory(email="later@acme.com"),
                start_at=now + timedelta(hours=5), created_at=now)

    with _window_held_open():
        assert next_first_email_at(now) == now + timedelta(hours=5)


def test_the_soonest_box_wins():
    """One free box is all a send needs, so the pool answers with its earliest."""
    now = timezone.now()
    maillog.mailbox("early@acme.com", next_send_at=now + timedelta(minutes=2))
    maillog.mailbox("late@acme.com", next_send_at=now + timedelta(hours=2))

    with _window_held_open():
        assert next_first_email_at(now) == now + timedelta(minutes=2)


def test_a_box_at_its_ceiling_is_asked_about_tomorrow():
    """Headroom is a per-day ledger, so a capped box is free when the day rolls — the
    difference between waiting four minutes and waiting for the morning."""
    now = timezone.now()
    maillog.mailbox(daily_limit=0)

    with _window_held_open(), patch(
            "openoutsend.emails.models.mailbox._local_midnight",
            return_value=now.replace(hour=0, minute=0, second=0, microsecond=0)):
        assert next_first_email_at(now).date() > now.date()


def test_the_window_is_applied_on_top_of_the_spacing_clock():
    """A box whose spacing elapses at 21:00 is not free at 21:00."""
    now = timezone.now()
    maillog.mailbox(next_send_at=now + timedelta(minutes=4))

    with patch("openoutsend.core.sending_window.next_window_open") as window:
        next_first_email_at(now)

    window.assert_called_once_with(now + timedelta(minutes=4), operator_timezone())
