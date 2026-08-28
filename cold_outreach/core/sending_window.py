# cold_outreach/core/sending_window.py
"""When a cold email may leave — the operator's working day, in their own time.

One question, asked at the last gate before a first email is written: is it a
weekday, and is it between 08:00 and 20:00 where the operator is? Openers wait
for the answer; replies never come through here at all.

The timezone is *derived*, never configured. The operator answered one question
at onboarding — their country — and ``pytz.country_timezones`` turns it into a
zone. Countries spanning several zones (US, BR, AU) get the first one the table
lists, which is a deliberate approximation: the point of the window is to keep
sends out of the middle of the night, and no zone within one country is more than
a few hours from any other. A second onboarding question buys, at most, the
difference between 08:00 and 11:00 Eastern.

Nothing here reads a lead's location. We know where the operator is; we never
know where the recipient is, and a campaign may target several countries at once.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone

from cold_outreach.core.business_time import is_business_day
from cold_outreach.core.conf import SEND_WINDOW_END_HOUR, SEND_WINDOW_START_HOUR

logger = logging.getLogger(__name__)

UTC = ZoneInfo("UTC")


def within_sending_window(now: datetime | None = None) -> bool:
    """True when a first email may leave right now — Mon–Fri, 08:00–20:00 operator-local."""
    local = timezone.localtime(now or timezone.now(), operator_timezone())
    return (
        is_business_day(local.date())
        and SEND_WINDOW_START_HOUR <= local.hour < SEND_WINDOW_END_HOUR
    )


def next_window_open(now: datetime | None = None) -> datetime:
    """The first instant from *now* onwards at which a cold email may leave.

    ``now`` itself when the window is already open, so a caller can take the maximum
    of this and any other clock without special-casing "already". Otherwise the next
    08:00 the operator will actually see: later this morning if the day has not
    started, and the next working day's morning if the evening or a weekend is in the
    way — a Friday 20:30 answer is Monday 08:00, not Saturday.

    ``within_sending_window`` answers *may I send now*; this answers *when may I*, and
    the second question only became worth asking when a run was allowed to wait for it
    (``send_job.py``). They read the same two constants, so a widened window moves both.
    """
    local = timezone.localtime(now or timezone.now(), operator_timezone())
    if within_sending_window(local):
        return local

    opening = local.replace(hour=SEND_WINDOW_START_HOUR, minute=0, second=0, microsecond=0)
    if opening <= local:
        opening += timedelta(days=1)
    while not is_business_day(opening.date()):
        opening += timedelta(days=1)
    return opening


def operator_timezone() -> ZoneInfo:
    """The operator's timezone, from their configured country code; UTC if unset."""
    from cold_outreach.core.operator import operator_country

    return _zone_for_country(operator_country())


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
