"""Business days forward and between: what every step's and conversation's wait counts in.

Fixed dates: 2026-10-09 is a Friday.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from openoutsend.core.business_time import add_business_days, business_days_between

FRIDAY = datetime.fromisoformat("2026-10-09T15:00:00+00:00")


def test_add_business_days_steps_over_the_weekend():
    assert add_business_days(FRIDAY, 1) == datetime.fromisoformat("2026-10-12T00:00:00+00:00")
    assert business_days_between(FRIDAY, add_business_days(FRIDAY, 3)) == 3


def test_add_business_days_of_zero_is_the_start_itself():
    assert add_business_days(FRIDAY, 0) == FRIDAY


def test_with_the_weekend_pause_on_friday_to_monday_is_one_day(settings):
    settings.OUTSEND_ENFORCE_WEEKEND_PAUSE = True

    assert business_days_between(FRIDAY, datetime.fromisoformat("2026-10-12T09:00:00+00:00")) == 1
    assert business_days_between(FRIDAY, FRIDAY + timedelta(days=15)) == 10


def test_with_the_weekend_pause_off_every_day_counts(settings):
    """An operator who sends at weekends waits in calendar days: Friday + 1 is Saturday."""
    settings.OUTSEND_ENFORCE_WEEKEND_PAUSE = False
    monday = datetime.fromisoformat("2026-10-12T09:00:00+00:00")

    assert add_business_days(FRIDAY, 1) == datetime.fromisoformat("2026-10-10T00:00:00+00:00")
    assert add_business_days(FRIDAY, 3) == datetime.fromisoformat("2026-10-12T00:00:00+00:00")
    assert business_days_between(FRIDAY, monday) == 3
    assert business_days_between(FRIDAY, FRIDAY + timedelta(days=15)) == 15


def test_working_days_are_counted_on_the_leads_calendar(settings):
    """Friday 22:00 UTC is already Saturday 09:00 in Sydney, so the next working day
    there is Monday in Sydney — Sunday 13:00 UTC — and Thursday evening UTC to Friday
    morning UTC is no working day at all in Sydney, where both are Friday."""
    settings.OUTSEND_ENFORCE_WEEKEND_PAUSE = True
    sydney = ZoneInfo("Australia/Sydney")
    friday_night = datetime.fromisoformat("2026-10-09T22:00:00+00:00")

    assert add_business_days(friday_night, 1, sydney) == datetime.fromisoformat("2026-10-11T13:00:00+00:00")
    assert add_business_days(friday_night, 1) == datetime.fromisoformat("2026-10-12T00:00:00+00:00")

    thursday_evening = datetime.fromisoformat("2026-10-08T20:00:00+00:00")
    friday_morning = datetime.fromisoformat("2026-10-09T10:00:00+00:00")
    assert business_days_between(thursday_evening, friday_morning, sydney) == 0
    assert business_days_between(thursday_evening, friday_morning) == 1
