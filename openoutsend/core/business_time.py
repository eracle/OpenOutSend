# openoutsend/core/business_time.py
"""Working-day arithmetic — how old a thread really is.

A thread that sat untouched over a weekend has not really gone quiet, and the
outreach agent is told the gap in working days so it reads a Friday reply answered
on Monday as one day old rather than three.

**Which days are working days is the operator's, through one flag.** With
``OUTSEND_ENFORCE_WEEKEND_PAUSE`` on (the default) they are Monday–Friday. An
operator who turns it off sends on Saturday and Sunday, so those are working days
too: every wait, step gap and silence is then counted in calendar days. One answer
for both questions, so the clock that lets a Saturday email leave and the clock that
says how long a Saturday silence lasted never disagree.

**The days are the lead's.** Both functions take the lead's zone
(``sending_window.lead_timezone``) and read the dates there, so a gap of N working
days skips the recipient's weekend: Friday evening in Sydney is still Friday morning
in Rome. Without a zone, a datetime's dates are read in its own offset.

Measuring is all this does. Public holidays are deliberately not modelled: they are
per-country, per-year data we do not carry, and this figure is coarse enough that a
missed holiday costs one slightly-off number in a prompt.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, tzinfo

from django.conf import settings

SATURDAY = 5


def weekend_pause_enforced() -> bool:
    """Whether Sat/Sun are shut (``OUTSEND_ENFORCE_WEEKEND_PAUSE``, default on)."""
    return getattr(settings, "OUTSEND_ENFORCE_WEEKEND_PAUSE", True)


def is_business_day(day: date) -> bool:
    """True for Monday–Friday, and for every day once the weekend pause is off."""
    return day.weekday() < SATURDAY or not weekend_pause_enforced()


def business_days_between(start: datetime, end: datetime, zone: tzinfo | None = None) -> int:
    """Whole working days elapsed from ``start`` to ``end``, with dates read in *zone*.

    Counts the business days in the half-open range ``(start.date(), end.date()]``,
    so Friday → Monday is 1 and Friday → Sunday is 0 under the weekend pause (3 and 2
    without it). Never negative.
    """
    start, end = _in_zone(start, zone), _in_zone(end, zone)
    return _business_days_in_range(start.date() + timedelta(days=1),
                                   end.date() + timedelta(days=1))


def add_business_days(start: datetime, days: int, zone: tzinfo | None = None) -> datetime:
    """The first moment ``days`` working days after ``start``, by ``business_days_between``.

    ``start`` itself for ``0``; otherwise midnight, in *zone* (``start``'s own when left
    out), of the ``days``-th working day after ``start``'s date there — the moment the
    count reaches ``days``.
    """
    if days <= 0:
        return start
    start = _in_zone(start, zone)
    day = start.date()
    counted = 0
    while counted < days:
        day += timedelta(days=1)
        counted += is_business_day(day)
    return datetime.combine(day, time.min, tzinfo=start.tzinfo)


# ── Internals ─────────────────────────────────────────────────────


def _in_zone(moment: datetime, zone: tzinfo | None) -> datetime:
    """*moment* as the clock in *zone* reads it — unchanged without one."""
    return moment.astimezone(zone) if zone is not None else moment


def _business_days_in_range(start: date, end: date) -> int:
    """Number of working days in the half-open range ``[start, end)``."""
    days = (end - start).days
    if days <= 0:
        return 0
    if not weekend_pause_enforced():
        return days
    full_weeks, remainder = divmod(days, 7)
    count = full_weeks * 5
    for offset in range(remainder):
        if is_business_day(start + timedelta(days=offset)):
            count += 1
    return count
