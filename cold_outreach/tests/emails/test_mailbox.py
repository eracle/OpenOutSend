# tests/emails/test_mailbox.py
"""The pool's two answers: whether a box may be stored, and when one may next send.

`create_verified` is the gate on storing a box — SMTP auth, because the provider has no
health API. `next_first_email_at` is the guard set `free_for_first_email` enforces, read
as a clock rather than as a boolean, which is what lets a bounded run wait for it.
"""
from datetime import timedelta

import pytest
from unittest.mock import patch

from django.utils import timezone

from cold_outreach.emails.models import Mailbox

_FIELDS = dict(
    from_address="joe@acme.com",
    password="app-pw",
    host="smtp.acme.com",
    port=465,
    imap_host="imap.acme.com",
    imap_port=993,
)


@pytest.mark.django_db
def test_create_verified_stores_box_when_auth_succeeds():
    with patch("cold_outreach.emails.smtp.verify_auth", return_value=(True, "")) as auth:
        box, reason = Mailbox.objects.create_verified(**_FIELDS)

    auth.assert_called_once_with("smtp.acme.com", 465, "joe@acme.com", "app-pw")
    assert reason == ""
    assert box is not None
    # the SMTP login is the address itself, and every field round-trips
    assert box.username == "joe@acme.com"
    assert box.from_address == "joe@acme.com"
    assert (box.host, box.port, box.imap_host, box.imap_port) == (
        "smtp.acme.com", 465, "imap.acme.com", 993,
    )
    assert Mailbox.objects.count() == 1


@pytest.mark.django_db
def test_create_verified_stores_nothing_when_auth_rejected():
    with patch("cold_outreach.emails.smtp.verify_auth", return_value=(False, "auth rejected (535)")):
        box, reason = Mailbox.objects.create_verified(**_FIELDS)

    assert box is None
    assert reason == "auth rejected (535)"
    assert Mailbox.objects.count() == 0


@pytest.mark.django_db
def test_create_verified_repairs_existing_box_in_place():
    with patch("cold_outreach.emails.smtp.verify_auth", return_value=(True, "")):
        Mailbox.objects.create_verified(**_FIELDS)
        box, _ = Mailbox.objects.create_verified(**{**_FIELDS, "password": "new-pw"})

    assert Mailbox.objects.count() == 1
    assert box.password == "new-pw"


# ── When the pool could next open a conversation ──────────────────


def _box(address="joe@acme.com", **kwargs):
    return Mailbox.objects.create(
        username=address, password="pw", from_address=address, **kwargs)


def _open_window():
    """Ask the clock with the window held open, so one gate is tested at a time."""
    return patch("cold_outreach.emails.models.mailbox.next_window_open",
                 side_effect=lambda moment: moment)


@pytest.mark.django_db
def test_no_mailbox_is_the_one_answer_no_clock_resolves():
    """None, not a time: waiting cannot connect a box, so a run must stop instead."""
    assert Mailbox.objects.next_first_email_at() is None


@pytest.mark.django_db
def test_a_free_box_could_send_now():
    _box()
    now = timezone.now()

    with _open_window():
        assert Mailbox.objects.next_first_email_at(now) == now


@pytest.mark.django_db
def test_a_spaced_box_is_free_when_its_clock_elapses():
    now = timezone.now()
    _box(next_send_at=now + timedelta(minutes=4))

    with _open_window():
        assert Mailbox.objects.next_first_email_at(now) == now + timedelta(minutes=4)


@pytest.mark.django_db
def test_an_elapsed_clock_does_not_answer_in_the_past():
    """A `next_send_at` from yesterday means *now*, not a moment already gone."""
    now = timezone.now()
    _box(next_send_at=now - timedelta(hours=3))

    with _open_window():
        assert Mailbox.objects.next_first_email_at(now) == now


@pytest.mark.django_db
def test_the_soonest_box_wins():
    """One free box is all a send needs, so the pool answers with its earliest."""
    now = timezone.now()
    _box("early@acme.com", next_send_at=now + timedelta(minutes=2))
    _box("late@acme.com", next_send_at=now + timedelta(hours=2))

    with _open_window():
        assert Mailbox.objects.next_first_email_at(now) == now + timedelta(minutes=2)


@pytest.mark.django_db
def test_a_box_at_its_ceiling_is_asked_about_tomorrow():
    """Headroom is a per-day ledger, so a capped box is free when the day rolls — the
    difference between waiting four minutes and waiting for the morning."""
    now = timezone.now()
    _box(daily_limit=0)

    with _open_window(), patch(
            "cold_outreach.emails.models.mailbox._local_midnight",
            return_value=now.replace(hour=0, minute=0, second=0, microsecond=0)):
        assert Mailbox.objects.next_first_email_at(now).date() > now.date()


@pytest.mark.django_db
def test_the_window_is_applied_on_top_of_the_spacing_clock():
    """A box whose spacing elapses at 21:00 is not free at 21:00 — the real composition,
    with the window left unmocked."""
    now = timezone.now()
    _box(next_send_at=now + timedelta(minutes=4))

    with patch("cold_outreach.emails.models.mailbox.next_window_open") as window:
        Mailbox.objects.next_first_email_at(now)

    window.assert_called_once_with(now + timedelta(minutes=4))
