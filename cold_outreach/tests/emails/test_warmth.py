from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from unittest.mock import patch

import pytest

from cold_outreach.core.conf import WARM_CEILING_SENDS, WARM_FLOOR_SENDS
from cold_outreach.emails.delivery_policy import Response
from cold_outreach.emails import warmth
from cold_outreach.emails.models import DeliveryEvent, Mailbox
from cold_outreach.emails.warmth import (
    capacity_from,
    mark_measured,
    measurement_due,
    refresh_capacity,
)
from cold_outreach.tests.emails import maillog


def _history(*daily_counts: int) -> Counter:
    """Sent history from a sequence of per-day totals, one day apart."""
    start = date(2026, 7, 1)
    return Counter({start + timedelta(days=i): n for i, n in enumerate(daily_counts)})


@pytest.fixture(autouse=True)
def _clear_measurement_cache():
    """The measurement cadence is a process-held date — reset it between tests."""
    warmth._measured_on = None
    yield
    warmth._measured_on = None


def _box(**kwargs) -> Mailbox:
    return maillog.mailbox("a@b.com", **kwargs)


def _verdict(box, response, smtp_code):
    """One recorded refusal against a send from *box*."""
    return DeliveryEvent.objects.create(
        message=maillog.outbound(box),
        status="deferred" if response == Response.DEFERRED else "error",
        response=response,
        smtp_code=smtp_code,
    )


class TestCapacityFrom:
    def test_no_history_yields_floor(self):
        assert capacity_from(Counter(), clean=True) == WARM_FLOOR_SENDS

    def test_steps_above_sustained_volume_when_clean(self):
        # p75 of [10, 10, 10, 10] is 10; a clean window allows the growth step.
        assert capacity_from(_history(10, 10, 10, 10), clean=True) == 15

    def test_holds_at_sustained_volume_when_not_clean(self):
        assert capacity_from(_history(10, 10, 10, 10), clean=False) == 10

    def test_idle_days_do_not_drag_the_measurement_down(self):
        # Weekends off must not read as reduced capacity: zero-days are excluded,
        # so this measures the same as four solid days.
        assert capacity_from(_history(10, 0, 10, 0, 10, 0, 10), clean=True) == 15

    def test_single_anomalous_day_does_not_set_capacity(self):
        # p75 of [5, 5, 5, 80] is 5 — one burst is not a demonstrated volume.
        assert capacity_from(_history(5, 5, 5, 80), clean=True) == 7

    def test_ceiling_is_a_hard_rail(self):
        assert capacity_from(_history(200, 200, 200), clean=True) == WARM_CEILING_SENDS

    def test_floor_survives_a_near_silent_box(self):
        assert capacity_from(_history(1), clean=True) == WARM_FLOOR_SENDS

    def test_growth_recovers_from_a_throttled_window(self):
        # A box knocked down to 5/day climbs back to full band in under a week,
        # which an additive step could not do.
        capacity, days = 5, 0
        while capacity < 40:
            capacity = capacity_from(_history(capacity), clean=True)
            days += 1
        assert days <= 7


@pytest.mark.django_db
class TestRefreshCapacity:
    def test_persists_the_measurement(self):
        box = _box()
        with patch("cold_outreach.emails.warmth.read_sent_history",
                   return_value=_history(20, 20, 20)):
            assert refresh_capacity(box) == 30
        box.refresh_from_db()
        assert box.daily_limit == 30

    def test_unreachable_box_keeps_its_last_measurement(self):
        box = _box(daily_limit=30)
        with patch("cold_outreach.emails.warmth.read_sent_history",
                   side_effect=OSError("connection reset")):
            assert refresh_capacity(box) == 30
        box.refresh_from_db()
        assert box.daily_limit == 30

    def test_receiver_pushback_holds_capacity_at_demonstrated_volume(self):
        box = _box()
        _verdict(box, Response.DEFERRED, 421)
        with patch("cold_outreach.emails.warmth.read_sent_history",
                   return_value=_history(20, 20, 20)):
            assert refresh_capacity(box) == 20

    def test_transport_failure_does_not_hold_capacity(self):
        # A dropped socket is not the receiver saying anything about this box;
        # a flaky network must not cost it capacity.
        box = _box()
        _verdict(box, Response.TRANSPORT, None)
        with patch("cold_outreach.emails.warmth.read_sent_history",
                   return_value=_history(20, 20, 20)):
            assert refresh_capacity(box) == 30

    def test_another_box_pushback_does_not_hold_this_one(self):
        box = _box()
        other = maillog.mailbox("c@d.com")
        _verdict(other, Response.DEFERRED, 421)
        with patch("cold_outreach.emails.warmth.read_sent_history",
                   return_value=_history(20, 20, 20)):
            assert refresh_capacity(box) == 30


@pytest.mark.django_db
class TestBouncingBoxSendsLess:
    """A box mailing dead addresses must send *less*, with nobody intervening.

    This is the signal the ramp could not previously receive at all: a bounce
    arrives hours later as ordinary mail, and delivery was only ever recorded from
    inside an exception handler. 590 sends produced 0 rows while the domain
    bounced itself onto a blocklist and capacity grew x1.5 a day.
    """

    def _box_with_bounces(self, sends: int, bounces: int) -> Mailbox:
        box = _box()
        for i in range(sends):
            send = maillog.outbound(box, message_id=f"s{i}@corp.com")
            maillog.accepted(send)
            if i < bounces:
                maillog.bounced(send)
        return box

    def test_a_clean_box_still_grows(self):
        box = self._box_with_bounces(sends=20, bounces=0)
        with patch("cold_outreach.emails.warmth.read_sent_history",
                   return_value=_history(20, 20, 20)):
            assert refresh_capacity(box) == 30

    def test_a_bouncing_box_is_cut_below_what_it_sustained(self):
        box = self._box_with_bounces(sends=20, bounces=4)   # 20% — far over tolerance
        with patch("cold_outreach.emails.warmth.read_sent_history",
                   return_value=_history(20, 20, 20)):
            # Not 30 (growth), not even 20 (hold): a bouncing box goes down.
            assert refresh_capacity(box) == 10

    def test_a_bounce_is_receiver_pushback(self):
        """Asynchronous failure reaches the growth gate, not just the cut."""
        box = self._box_with_bounces(sends=100, bounces=1)  # 1% — under tolerance
        with patch("cold_outreach.emails.warmth.read_sent_history",
                   return_value=_history(20, 20, 20)):
            assert refresh_capacity(box) == 20


class TestMeasurementCadence:
    def test_due_before_the_first_pass(self):
        assert measurement_due()

    def test_not_due_again_the_same_day(self):
        mark_measured()
        assert not measurement_due()

    def test_due_again_the_next_day(self):
        mark_measured()
        with patch("cold_outreach.emails.warmth.timezone.localdate",
                   return_value=date(2099, 1, 1)):
            assert measurement_due()
