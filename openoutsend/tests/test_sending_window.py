from __future__ import annotations

from datetime import datetime, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from openoutsend.core.sending_window import (
    UTC,
    _zone_for_country,
    lead_timezone,
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
    with patch("openoutsend.core.sending_window.operator_timezone",
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


class TestTheTwoFlagsAreIndependent:
    """`OUTSEND_ENFORCE_WORK_HOURS` and `OUTSEND_ENFORCE_WEEKEND_PAUSE` gate their own
    half of the window and nothing else — an operator can drop either alone."""

    def test_weekend_pause_off_still_holds_the_hours(self, settings):
        settings.OUTSEND_ENFORCE_WEEKEND_PAUSE = False
        assert _in_rome(_utc(SAT, 11)) is True    # Saturday noon Rome — now allowed
        assert _in_rome(_utc(SAT, 2)) is False    # Saturday 03:00 Rome — hours still hold

    def test_work_hours_off_still_holds_the_weekend(self, settings):
        settings.OUTSEND_ENFORCE_WORK_HOURS = False
        assert _in_rome(_utc(WED, 2)) is True     # Wednesday 03:00 Rome — hours no longer checked
        assert _in_rome(_utc(SAT, 11)) is False   # Saturday — weekend pause still holds

    def test_both_off_is_wide_open(self, settings):
        settings.OUTSEND_ENFORCE_WORK_HOURS = False
        settings.OUTSEND_ENFORCE_WEEKEND_PAUSE = False
        assert _in_rome(_utc(SAT, 2)) is True


class TestWhenItOpensAgain:
    """`within_sending_window` answers *may I now*; this answers *when may I* — the
    question a run that is allowed to wait for the window has to ask."""

    def _rome(self, now: datetime) -> datetime:
        with patch("openoutsend.core.sending_window.operator_timezone",
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

    def test_weekend_pause_off_a_friday_evening_opens_saturday_morning(self, settings):
        """With the weekend pause off, the wait no longer walks past Saturday."""
        settings.OUTSEND_ENFORCE_WEEKEND_PAUSE = False
        opens = self._rome(_utc(SAT - 1, 21))          # 22:00 Rome, Friday
        assert (opens.day, opens.hour) == (SAT, 8)

    def test_work_hours_off_a_weekend_night_opens_at_the_next_business_day_now(self, settings):
        """With hours off, only the weekend walk applies — no jump to 08:00."""
        settings.OUTSEND_ENFORCE_WORK_HOURS = False
        opens = self._rome(_utc(SAT, 3))               # 04:00 Rome, Saturday
        assert (opens.day, opens.hour, opens.weekday()) == (SAT + 2, 4, 0)


class TestTheLeadsOwnClock:
    """The window is read where the lead is; the operator sits in Rome (UTC+1 here)."""

    def _open_for(self, now: datetime, zone: str) -> bool:
        with patch("openoutsend.core.sending_window.operator_timezone",
                   return_value=ZoneInfo("Europe/Rome")):
            return within_sending_window(now, ZoneInfo(zone))

    def test_a_lead_west_of_the_operator_waits_for_their_morning(self):
        """12:00 Rome is 04:00 in Los Angeles (UTC-7 after the US switch)."""
        assert self._open_for(_utc(WED, 11), "America/Los_Angeles") is False
        assert self._open_for(_utc(WED, 17), "America/Los_Angeles") is True   # 10:00 LA

    def test_a_lead_east_of_the_operator_is_shut_by_their_evening(self):
        """19:00 Rome is 23:30 in Mumbai; 05:00 Rome is 09:30 there."""
        assert self._open_for(_utc(WED, 18), "Asia/Kolkata") is False
        assert self._open_for(_utc(WED, 4), "Asia/Kolkata") is True

    def test_a_lead_past_the_date_line_reads_their_own_weekday(self):
        """Sunday 20:00 UTC is already Monday 09:00 in Auckland (UTC+13), and Friday
        10:00 UTC is Friday 23:00 there."""
        assert self._open_for(_utc(SUN, 20), "Pacific/Auckland") is True
        assert self._open_for(_utc(SAT - 1, 10), "Pacific/Auckland") is False

    def test_the_wait_is_for_the_leads_morning(self):
        with patch("openoutsend.core.sending_window.operator_timezone",
                   return_value=ZoneInfo("Europe/Rome")):
            opens = next_window_open(_utc(WED, 11), ZoneInfo("America/Los_Angeles"))
        assert opens == _utc(WED, 15)   # 08:00 Los Angeles

    def test_a_lead_with_no_zone_reads_the_operators(self, settings):
        settings.OUTSEND_OPERATOR_COUNTRY = "it"
        assert lead_timezone(SimpleNamespace(timezone="")) == ZoneInfo("Europe/Rome")

    def test_a_lead_with_a_zone_reads_it(self, settings):
        settings.OUTSEND_OPERATOR_COUNTRY = "it"
        assert lead_timezone(SimpleNamespace(timezone="Asia/Kolkata")) == ZoneInfo("Asia/Kolkata")

    def test_a_zone_the_database_lost_falls_back_to_the_operators(self, settings):
        settings.OUTSEND_OPERATOR_COUNTRY = "it"
        assert lead_timezone(SimpleNamespace(timezone="Mars/Olympus")) == ZoneInfo("Europe/Rome")


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
