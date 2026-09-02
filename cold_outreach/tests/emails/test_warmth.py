from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from unittest.mock import patch

import pytest

from django.utils import timezone

from cold_outreach.core.conf import WARM_CEILING_SENDS, WARM_FLOOR_SENDS
from cold_outreach.emails.delivery_policy import Response
from cold_outreach.emails.models import DeliveryEvent, Mailbox
from cold_outreach.emails.warmth import capacity_from, measure_pool, refresh_capacity
from cold_outreach.tests.emails import maillog


def _history(*daily_counts: int) -> Counter:
    """Sent history from a sequence of per-day totals, one day apart."""
    start = date(2026, 7, 1)
    return Counter({start + timedelta(days=i): n for i, n in enumerate(daily_counts)})


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
        assert capacity_from(_history(10, 10, 10, 10), clean=True, current=10) == 12

    def test_holds_at_sustained_volume_when_not_clean(self):
        assert capacity_from(_history(10, 10, 10, 10), clean=False, current=10) == 10

    def test_idle_days_do_not_drag_the_measurement_down(self):
        # Weekends off must not read as reduced capacity: zero-days are excluded,
        # so this measures the same as four solid days.
        assert capacity_from(_history(10, 0, 10, 0, 10, 0, 10), clean=True, current=10) == 12

    def test_single_anomalous_day_does_not_set_capacity(self):
        # p75 of [5, 5, 5, 80] is 5 — one burst is not a demonstrated volume.
        assert capacity_from(_history(5, 5, 5, 80), clean=True, current=5) == 6

    def test_ceiling_is_a_hard_rail(self):
        assert capacity_from(_history(200, 200, 200), clean=True,
                             current=WARM_CEILING_SENDS) == WARM_CEILING_SENDS

    def test_floor_survives_a_near_silent_box(self):
        assert capacity_from(_history(1), clean=True, current=5) == WARM_FLOOR_SENDS


class TestTheRamp:
    """Growth is capped at a step above *yesterday's allowance*, not just above
    demonstrated volume. Without that, a box's own Sent folder — mostly a human's
    personal mail — hands it full cold volume on day one, which is the pattern
    receivers are looking for."""

    def test_a_fat_sent_folder_does_not_skip_the_ramp(self):
        # 40/day of history, but the box has never been allowed more than the floor:
        # it gets one rung, not the history.
        assert capacity_from(_history(40, 40, 40), clean=True, current=WARM_FLOOR_SENDS) == 6

    def test_an_unmeasured_box_starts_on_the_floor(self):
        # No `current` at all is the same case: a box nobody has measured is on the
        # bottom rung, whatever it has been carrying.
        assert capacity_from(_history(40, 40, 40), clean=True) == 6

    def test_a_drop_is_not_rationed(self):
        # The rung caps growth only. A receiver's verdict lands in full, today.
        assert capacity_from(_history(10, 10, 10), clean=False, current=50) == 10

    def test_reaching_a_working_volume_takes_about_three_weeks(self):
        """The ramp the field converges on: ~65–80/day after three weeks, not after one.

        Counted in *sending* days, and the window is Mon–Fri — so fourteen of them is
        about three calendar weeks. The old ×1.5 reached the same volume in eight.
        """
        capacity, days = WARM_FLOOR_SENDS, 0
        while capacity < 65:
            capacity = capacity_from(_history(capacity), clean=True, current=capacity)
            days += 1
        assert 12 <= days <= 18

    def test_a_throttled_box_still_climbs_back(self):
        # The step is multiplicative for this reason: knocked down to the floor, a box
        # recovers on its own rather than needing weeks of additive steps.
        assert capacity_from(_history(5), clean=True, current=5) > WARM_FLOOR_SENDS


@pytest.mark.django_db
class TestRefreshCapacity:
    def test_persists_the_measurement(self):
        box = _box(daily_limit=20)
        with patch("cold_outreach.emails.warmth.read_sent_history",
                   return_value=_history(20, 20, 20)):
            assert refresh_capacity(box) == 25
        box.refresh_from_db()
        assert box.daily_limit == 25

    def test_stamps_the_day_it_measured(self):
        box = _box(daily_limit=20)
        with patch("cold_outreach.emails.warmth.read_sent_history",
                   return_value=_history(20, 20, 20)):
            refresh_capacity(box)
        box.refresh_from_db()
        assert box.measured_on == timezone.localdate()

    def test_unreachable_box_keeps_its_last_measurement(self):
        box = _box(daily_limit=30)
        with patch("cold_outreach.emails.warmth.read_sent_history",
                   side_effect=OSError("connection reset")):
            assert refresh_capacity(box) == 30
        box.refresh_from_db()
        assert box.daily_limit == 30

    def test_unreachable_box_is_stamped_anyway(self):
        """One IMAP timeout a day, not one per pass."""
        box = _box(daily_limit=30)
        with patch("cold_outreach.emails.warmth.read_sent_history",
                   side_effect=OSError("connection reset")):
            refresh_capacity(box)
        box.refresh_from_db()
        assert box.measured_on == timezone.localdate()

    def test_receiver_pushback_holds_capacity_at_demonstrated_volume(self):
        box = _box(daily_limit=20)
        _verdict(box, Response.DEFERRED, 421)
        with patch("cold_outreach.emails.warmth.read_sent_history",
                   return_value=_history(20, 20, 20)):
            assert refresh_capacity(box) == 20

    def test_transport_failure_does_not_hold_capacity(self):
        # A dropped socket is not the receiver saying anything about this box;
        # a flaky network must not cost it capacity.
        box = _box(daily_limit=20)
        _verdict(box, Response.TRANSPORT, None)
        with patch("cold_outreach.emails.warmth.read_sent_history",
                   return_value=_history(20, 20, 20)):
            assert refresh_capacity(box) == 25

    def test_another_box_pushback_does_not_hold_this_one(self):
        box = _box(daily_limit=20)
        other = maillog.mailbox("c@d.com")
        _verdict(other, Response.DEFERRED, 421)
        with patch("cold_outreach.emails.warmth.read_sent_history",
                   return_value=_history(20, 20, 20)):
            assert refresh_capacity(box) == 25


@pytest.mark.django_db
class TestBouncingBoxSendsLess:
    """A box mailing dead addresses must send *less*, with nobody intervening.

    This is the signal the ramp could not previously receive at all: a bounce
    arrives hours later as ordinary mail, and delivery was only ever recorded from
    inside an exception handler. 590 sends produced 0 rows while the domain
    bounced itself onto a blocklist and capacity grew x1.5 a day.
    """

    def _box_with_bounces(self, sends: int, bounces: int) -> Mailbox:
        box = _box(daily_limit=20)
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
            assert refresh_capacity(box) == 25

    def test_a_bouncing_box_is_cut_below_what_it_sustained(self):
        box = self._box_with_bounces(sends=20, bounces=4)   # 20% — far over tolerance
        with patch("cold_outreach.emails.warmth.read_sent_history",
                   return_value=_history(20, 20, 20)):
            # Not 25 (growth), not even 20 (hold): a bouncing box goes down.
            assert refresh_capacity(box) == 10

    def test_a_bounce_is_receiver_pushback(self):
        """Asynchronous failure reaches the growth gate, not just the cut."""
        box = self._box_with_bounces(sends=100, bounces=1)  # 1% — under tolerance
        with patch("cold_outreach.emails.warmth.read_sent_history",
                   return_value=_history(20, 20, 20)):
            assert refresh_capacity(box) == 20


@pytest.mark.django_db
class TestMeasurementCadence:
    """Once a day per box, and the day is on the row.

    `outsend send` is one pass in a fresh process, so a process-held date would mean a
    full IMAP pass on every invocation — hundreds of logins a day under a timer.
    """

    def _measured(self, box) -> bool:
        """Run a pool pass over a reachable box; did this one get read?"""
        with patch("cold_outreach.emails.warmth.read_sent_history",
                   return_value=_history(20, 20, 20)) as read:
            measure_pool()
        return read.called

    def test_a_never_measured_box_is_measured(self):
        assert self._measured(_box(daily_limit=20))

    def test_a_box_measured_today_is_left_alone(self):
        box = _box(daily_limit=20, measured_on=timezone.localdate())
        assert not self._measured(box)
        box.refresh_from_db()
        assert box.daily_limit == 20

    def test_a_box_measured_yesterday_is_measured_again(self):
        yesterday = timezone.localdate() - timedelta(days=1)
        assert self._measured(_box(daily_limit=20, measured_on=yesterday))

    def test_every_box_in_the_pool_is_measured(self):
        _box(daily_limit=20)
        maillog.mailbox("c@d.com", daily_limit=20)
        with patch("cold_outreach.emails.warmth.read_sent_history",
                   return_value=_history(20, 20, 20)) as read:
            measure_pool()
        assert read.call_count == 2
