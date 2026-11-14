# openoutsend/core/sending_window.py
"""When a cold email may leave — the recipient's working day, in their own time.

Two questions, asked at the last gate before a cold email is written: is it
between 08:00 and 20:00 where the lead is, and is it a weekday there? Cold sends
wait for the answer; replies never come through here at all.

**The zone is the lead's when the record carried one** (``Lead.timezone``, an IANA
name the producer chose), and the operator's otherwise. Nothing here turns a
lead's country or city into a zone: that is the producer's job, done once with
whatever it knows, and a guess made on this side would be a second guess nobody
could correct.

The two are independent flags (``OUTSEND_ENFORCE_WORK_HOURS``,
``OUTSEND_ENFORCE_WEEKEND_PAUSE``), both on by default. An operator who wants
Saturday/Sunday sends but still wants the 08:00–20:00 line held turns off only
the second — a single combined switch could not express that. The weekend flag is
read in ``business_time.is_business_day``, so turning it off also makes the waits
between steps and follow-ups count Saturday and Sunday.

The operator's zone is derived from their country: ``pytz.country_timezones`` turns
the one onboarding answer into a zone, and countries spanning several zones (US, BR,
AU, ES) get the first one the table lists. An operator for whom that first zone is
wrong — the Canary Islands are an hour behind Madrid — names theirs in
``OUTSEND_OPERATOR_TIMEZONE``, which wins over the country. The operator's zone is
the fallback for a lead with none, and it also draws the midnight every daily cap
counts from (``emails/models/mailbox._local_midnight``).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.utils import timezone

from openoutsend.core.business_time import is_business_day
from openoutsend.core.conf import SEND_WINDOW_END_HOUR, SEND_WINDOW_START_HOUR

logger = logging.getLogger(__name__)

UTC = ZoneInfo("UTC")


def within_sending_window(now: datetime | None = None, zone: ZoneInfo | None = None) -> bool:
    """True when a cold email may leave right now — Mon–Fri, 08:00–20:00 in *zone*.

    *zone* is the recipient's (``lead_timezone``); left out, it is the operator's.
    Either half is skipped when its flag is off: with ``OUTSEND_ENFORCE_WEEKEND_PAUSE``
    disabled, Saturday and Sunday pass this check like any other day; with
    ``OUTSEND_ENFORCE_WORK_HOURS`` disabled, every hour does.
    """
    local = timezone.localtime(now or timezone.now(), zone or operator_timezone())
    return (
        is_business_day(local.date())  # every day is one with the weekend pause off
        and (not _work_hours_enforced()
             or SEND_WINDOW_START_HOUR <= local.hour < SEND_WINDOW_END_HOUR)
    )


def next_window_open(now: datetime | None = None, zone: ZoneInfo | None = None) -> datetime:
    """The first instant from *now* onwards at which a cold email may leave for *zone*.

    ``now`` itself when the window is already open, so a caller can take the maximum
    of this and any other clock without special-casing "already". Otherwise the next
    08:00 in *zone* (the operator's when left out): later this morning if the day has
    not started, and the next working day's morning if the evening or a weekend is in
    the way — a Friday 20:30 answer is Monday 08:00, not Saturday.

    ``within_sending_window`` answers *may I send now*; this answers *when may I*, and
    the second question only became worth asking when a run was allowed to wait for it
    (``send_job.py``). They read the same two constants and the same two flags, so a
    widened window — or a disabled flag — moves both.
    """
    local = timezone.localtime(now or timezone.now(), zone or operator_timezone())
    if within_sending_window(local, zone):
        return local

    if _work_hours_enforced():
        opening = local.replace(hour=SEND_WINDOW_START_HOUR, minute=0, second=0, microsecond=0)
        if opening <= local:
            opening += timedelta(days=1)
    else:
        opening = local

    while not is_business_day(opening.date()):
        opening += timedelta(days=1)
    return opening


def _work_hours_enforced() -> bool:
    """Whether the 08:00–20:00 half of the window is checked (``OUTSEND_ENFORCE_WORK_HOURS``)."""
    return getattr(settings, "OUTSEND_ENFORCE_WORK_HOURS", True)


def lead_timezone(lead) -> ZoneInfo:
    """The zone *lead* reads the clock in: their own when the record carried one, else the operator's.

    Ingest only stores a name ``ZoneInfo`` can load (``leads/ingest.py``), so a stored
    name that no longer loads means the system's tz database lost it — rare enough that
    falling back to the operator's clock, which is how a blank one is read, is the answer.
    """
    name = getattr(lead, "timezone", "")
    zone = load_zone(name) if name else None
    return zone or operator_timezone()


def load_zone(name: str) -> ZoneInfo | None:
    """The IANA zone called *name*, or ``None`` when the tz database has no such zone."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def operator_timezone() -> ZoneInfo:
    """The operator's timezone: ``OUTSEND_OPERATOR_TIMEZONE`` when set, else their country's; UTC if neither.

    Raises ``OutsendError`` for a zone name the tz database does not know: a typo read
    as UTC would move every send by hours without a word.
    """
    from openoutsend.core.operator import operator_country
    from openoutsend.errors import OutsendError

    name = (getattr(settings, "OUTSEND_OPERATOR_TIMEZONE", "") or "").strip()
    if not name:
        return _zone_for_country(operator_country())
    zone = load_zone(name)
    if zone is None:
        raise OutsendError(
            f"OUTSEND_OPERATOR_TIMEZONE must be an IANA zone name like Atlantic/Canary, not {name!r}")
    return zone


def _zone_for_country(country_code: str | None) -> ZoneInfo:
    """First zone ``pytz`` lists for an ISO 3166 alpha-2 code, or UTC if unknown.

    UTC is the honest fallback for a missing code, not a guess dressed as one: an
    empty value means nobody has configured a country yet rather than that we failed
    to resolve one.
    """
    import pytz

    zones = pytz.country_timezones.get((country_code or "").upper())
    if not zones:
        return UTC
    return ZoneInfo(zones[0])
