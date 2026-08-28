from __future__ import annotations

from datetime import datetime, timezone as dt_timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from cold_outreach.core.sending_window import (
    UTC,
    _zone_for_country,
    next_window_open,
    operator_timezone,
    within_sending_window,
)

# Reference week: Wed 2026-03-18 … Sat 2026-03-21, stated in UTC.
def _utc(day: int, hour: int) -> datetime:
    return datetime(2026, 3, day, hour, 0, tzinfo=dt_timezone.utc)


WED = 18
SAT = 21
SUN = 22


def _in_rome(now: datetime) -> bool:
    """Ask the window as an operator sitting in Italy (UTC+1 on this date)."""
    with patch("cold_outreach.core.sending_window.operator_timezone",
               return_value=ZoneInfo("Europe/Rome")):
        return within_sending_window(now)


class TestTheWorkingDay:
    def test_midday_is_open(self):
        assert _in_rome(_utc(WED, 11)) is True   # 12:00 Rome

    def test_opens_at_eight_local(self):
        assert _in_rome(_utc(WED, 6)) is False   # 07:00 Rome
        assert _in_rome(_utc(WED, 7)) is True    # 08:00 Rome — inclusive

    def test_closes_at_twenty_local(self):
        assert _in_rome(_utc(WED, 18)) is True   # 19:00 Rome
        assert _in_rome(_utc(WED, 19)) is False  # 20:00 Rome — exclusive

    def test_the_middle_of_the_night_is_shut(self):
        assert _in_rome(_utc(WED, 2)) is False   # 03:00 Rome

    def test_the_operators_clock_decides_not_utc(self):
        """21:30 UTC is 22:30 in Rome — shut — while 07:30 UTC is 08:30 — open.

        The whole point of resolving a zone: a window read off UTC would open and
        close at the wrong hour for every operator who is not in Britain.
        """
        assert _in_rome(_utc(WED, 21)) is False
        assert _in_rome(_utc(WED, 7)) is True


class TestWeekends:
    def test_saturday_is_shut_at_every_hour(self):
        assert _in_rome(_utc(SAT, 11)) is False

    def test_sunday_is_shut(self):
        assert _in_rome(_utc(SUN, 11)) is False

    def test_the_weekend_is_read_in_local_time(self):
        """23:00 UTC Friday is already Saturday in Rome, and stays shut."""
        assert _in_rome(_utc(SAT - 1, 23)) is False


class TestWhenItOpensAgain:
    """`within_sending_window` answers *may I now*; this answers *when may I* — the
    question a run that is allowed to wait for the window has to ask."""

    def _rome(self, now: datetime) -> datetime:
        with patch("cold_outreach.core.sending_window.operator_timezone",
                   return_value=ZoneInfo("Europe/Rome")):
            return next_window_open(now).astimezone(ZoneInfo("Europe/Rome"))

    def test_an_open_window_answers_now(self):
        """So a caller can take the maximum of this and a spacing clock without a branch."""
        assert self._rome(_utc(WED, 11)).hour == 12   # 12:00 Rome, unchanged

    def test_before_the_morning_it_is_this_morning(self):
        opens = self._rome(_utc(WED, 2))              # 03:00 Rome
        assert (opens.day, opens.hour, opens.minute) == (WED, 8, 0)

    def test_after_the_evening_it_is_tomorrow_morning(self):
        opens = self._rome(_utc(WED, 21))             # 22:00 Rome
        assert (opens.day, opens.hour) == (WED + 1, 8)

    def test_a_friday_evening_is_monday_morning(self):
        """The wait skips the weekend rather than answering Saturday 08:00."""
        opens = self._rome(_utc(SAT - 1, 21))         # 22:00 Rome, Friday
        assert (opens.day, opens.hour, opens.weekday()) == (SAT + 2, 8, 0)

    def test_saturday_dawn_is_still_monday(self):
        """Early enough that today's 08:00 has not passed — and still not a working day."""
        opens = self._rome(_utc(SAT, 3))              # 04:00 Rome, Saturday
        assert (opens.day, opens.hour, opens.weekday()) == (SAT + 2, 8, 0)


class TestResolvingTheZone:
    def test_a_single_zone_country(self):
        assert _zone_for_country("it") == ZoneInfo("Europe/Rome")

    def test_case_does_not_matter(self):
        """Onboarding stores the code lowercased; pytz keys it uppercase."""
        assert _zone_for_country("IT") == _zone_for_country("it")

    def test_a_multi_zone_country_takes_the_first(self):
        assert _zone_for_country("us") == ZoneInfo("America/New_York")

    def test_an_unknown_code_falls_back_to_utc(self):
        assert _zone_for_country("xx") == UTC

    def test_no_code_at_all_falls_back_to_utc(self):
        assert _zone_for_country("") == UTC
        assert _zone_for_country(None) == UTC

    def test_reads_the_operators_configured_country(self, settings):
        """The country is a setting on this side — there is no config table to load."""
        settings.OUTSEND_OPERATOR_COUNTRY = "de"
        assert operator_timezone() == ZoneInfo("Europe/Berlin")
