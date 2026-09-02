# cold_outreach/emails/warmth.py
"""Measure a mailbox's warm capacity from its own send history.

The per-box daily ceiling is not declared, it is read off the box: IMAP the Sent
folder, bucket the messages by day, and let what the box has demonstrably
sustained decide what it may send today. This is the only warmth signal
available at cold-outreach volume — Google Postmaster Tools suppresses its
metrics below roughly 100 messages/day to Gmail, an order above where a safe
cold sender operates, and seed-list placement tests measure a panel rather than
the box.

Reading the Sent folder (rather than counting our own mail-log rows) is
deliberate: the receiver counts *every* message the box emits — a human's mail,
a provider's warmup traffic — and a ceiling derived from our own ledger alone
would be blind to all of it.

What the box has sustained is not the only bound. Growth is also capped at a step
above the box's *previous* allowance, which is what makes this a warmup rather
than a reading: a Sent folder is mostly a human's own mail, receivers judge that
differently from cold volume, and a box that jumps from nothing to full volume in
a day is the pattern they are looking for. Every box therefore starts on the
floor and climbs. Only growth is capped — a measurement pointing downwards is a
receiver telling us something, and it applies today.

The measurement is a cache, never state: it is recomputed from the box each day
and can be discarded at any time without losing anything. The value lives on
``Mailbox.daily_limit`` because reading it costs an IMAP round trip and the send
path consults it constantly, and the date of the last read lives beside it on
``Mailbox.measured_on`` — on the row rather than in the process, because
``outsend send`` is one pass in a fresh process and a process-held date would
re-measure the pool on every invocation. An unreachable box keeps its last
measurement rather than falling to zero — a flaky network is not evidence about
reputation.
"""
from __future__ import annotations

import email
import imaplib
import logging
from collections import Counter
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

from django.utils import timezone

from cold_outreach.core.conf import (
    WARM_BOUNCE_TOLERANCE,
    WARM_CEILING_SENDS,
    WARM_FLOOR_SENDS,
    WARM_GROWTH_FACTOR,
    WARM_HISTORY_DAYS,
)

logger = logging.getLogger(__name__)

IMAP_TIMEOUT_SECONDS = 30

def measure_pool() -> None:
    """Re-measure every box that has not been measured today.

    The pool's whole cadence: capacity is a daily quantity, so a box is read once a
    day and left alone for the rest of it. Called from the send pass, which fires
    every few minutes under send pacing — measuring on each would be hundreds of
    IMAP logins a day.

    A box that could not be reached is still stamped, so an unreachable mailbox
    costs one IMAP timeout a day rather than one per pass.
    """
    from cold_outreach.emails.models import Mailbox

    for mailbox in Mailbox.objects.exclude(measured_on=timezone.localdate()):
        refresh_capacity(mailbox)


def refresh_capacity(mailbox) -> int:
    """Re-measure *mailbox*'s warm capacity and persist it; return the new value.

    Falls back to the stored measurement when the box can't be read, so a network
    blip never silently throttles a healthy mailbox.
    """
    today = timezone.localdate()
    try:
        history = read_sent_history(mailbox)
    except (imaplib.IMAP4.error, OSError) as exc:
        logger.warning("warmth: could not read %s (%s) — keeping %d/day",
                       mailbox.from_address, exc, mailbox.daily_limit)
        mailbox.measured_on = today
        mailbox.save(update_fields=["measured_on"])
        return mailbox.daily_limit

    capacity = capacity_from(
        history,
        clean=not _receiver_pushed_back(mailbox),
        bouncing=_is_bouncing(mailbox),
        current=mailbox.daily_limit,
    )
    mailbox.daily_limit = capacity
    mailbox.measured_on = today
    mailbox.save(update_fields=["daily_limit", "measured_on"])
    logger.info("warmth: %s measured at %d/day (%d day(s) of history)",
                mailbox.from_address, capacity, len(history))
    return capacity


# ── Measurement ───────────────────────────────────────────────────────


def capacity_from(history: Counter, *, clean: bool, bouncing: bool = False,
                  current: int = WARM_FLOOR_SENDS) -> int:
    """Daily capacity implied by a date → sends-that-day mapping.

    ``clean`` is the growth gate: with no failed send in the recent window the
    box may step above what it has sustained, otherwise it holds there. Empty
    history (a box that has never sent) yields the floor.

    ``bouncing`` is the only rule here that points *downwards*. Holding volume
    steady is the right answer to a receiver pacing us, and the wrong one to a
    box mailing dead addresses: a bounce rate above the tolerance is the box
    damaging itself, and it must send **less** without waiting for a human to
    notice. Halving rather than stopping, because the same evidence at the same
    rate halves it again tomorrow, and a box that stops sending stops producing
    the evidence that would let it recover.

    ``current`` is yesterday's allowance, and it is what makes this a ramp rather
    than a jump. The Sent folder is a record of everything the box emits, most of
    it a human's own mail, and a box that carries 20 personal messages a day has
    not thereby demonstrated it can carry 20 cold ones — receivers judge the two
    differently, and a mailbox that goes from nothing to full volume in a day is
    the exact shape they are looking for. So growth is capped at the same factor
    above what the box was *already allowed*, and a box nobody has measured before
    starts on the floor however long its history. It bounds growth only: a
    measurement that lands below the current allowance is a receiver telling us
    something, and that is applied in full, today.
    """
    active_days = sorted(n for n in history.values() if n > 0)
    if not active_days:
        return WARM_FLOOR_SENDS

    sustained = _percentile(active_days, 75)
    allowed = sustained * WARM_GROWTH_FACTOR if clean else sustained
    if bouncing:
        allowed = allowed / 2
    allowed = min(allowed, current * WARM_GROWTH_FACTOR)
    return int(max(WARM_FLOOR_SENDS, min(WARM_CEILING_SENDS, allowed)))


def _percentile(sorted_values: list[int], pct: int) -> float:
    """Nearest-rank percentile of an already-sorted, non-empty list."""
    rank = max(1, round(pct / 100 * len(sorted_values)))
    return sorted_values[rank - 1]


def _receiver_pushed_back(mailbox) -> bool:
    """True when the receiver rejected a send from this box in the last day.

    Only verdicts the receiving server actually issued count
    (``delivery_policy.RECEIVER_RESPONSES``). A dropped socket or a bad password
    also fails a send, but neither is the receiver saying anything about this
    mailbox — letting them gate growth would mean a flaky network throttling a
    healthy box.

    A **bounce** is such a verdict, and it is the one this could not previously
    see: it arrives hours later as ordinary mail rather than as an exception, so
    while delivery was recorded only from inside an exception handler the ramp
    was blind to exactly the failure that matters most.
    """
    from cold_outreach.emails.delivery_policy import RECEIVER_RESPONSES
    from cold_outreach.emails.models import DeliveryEvent

    return DeliveryEvent.objects.filter(
        message__mailbox=mailbox,
        response__in=RECEIVER_RESPONSES,
        occurred_at__gte=timezone.now() - timedelta(days=1),
    ).exists()


def _is_bouncing(mailbox) -> bool:
    """True when this box's bounce rate is above what a healthy sender runs at."""
    from cold_outreach.emails.report import bounce_rate

    rate = bounce_rate(mailbox)
    if rate <= WARM_BOUNCE_TOLERANCE:
        return False
    logger.warning("warmth: %s is bouncing at %.1f%% — halving its capacity",
                   mailbox.from_address, rate * 100)
    return True


# ── IMAP transport ────────────────────────────────────────────────────


def read_sent_history(mailbox) -> Counter:
    """``{local date: messages sent}`` over the trailing window, from the box's
    Sent folder. Headers only — no bodies are fetched, and the folder is opened
    read-only."""
    imap = imaplib.IMAP4_SSL(mailbox.imap_host, mailbox.imap_port,
                             timeout=IMAP_TIMEOUT_SECONDS)
    try:
        imap.login(mailbox.username, mailbox.password)
        folder = _sent_folder(imap)
        if folder is None:
            logger.warning("warmth: no \\Sent folder on %s", mailbox.from_address)
            return Counter()
        return _count_by_day(imap, folder)
    finally:
        _logout(imap)


def _sent_folder(imap) -> str | None:
    """The Sent folder's name, found by its ``\\Sent`` special-use attribute.

    Matching the attribute rather than a name is what makes this work on a box
    in any language — Gmail localizes ``[Gmail]/Sent Mail``, the attribute is
    invariant.
    """
    status, data = imap.list()
    if status != "OK":
        return None
    for raw in data:
        line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        if r"\Sent" in line:
            return line.rsplit(' "', 1)[-1].strip('"')
    return None


def _count_by_day(imap, folder: str) -> Counter:
    """Sends per local date in *folder* over the trailing window."""
    status, _ = imap.select(f'"{folder}"', readonly=True)
    if status != "OK":
        return Counter()

    since = (datetime.now() - timedelta(days=WARM_HISTORY_DAYS)).strftime("%d-%b-%Y")
    status, data = imap.search(None, f"(SINCE {since})")
    if status != "OK" or not data or not data[0]:
        return Counter()

    counts = Counter()
    nums = data[0].split()
    for i in range(0, len(nums), 200):
        chunk = b",".join(nums[i:i + 200])
        status, response = imap.fetch(chunk, "(BODY.PEEK[HEADER.FIELDS (DATE)])")
        if status != "OK":
            continue
        for item in response:
            if not isinstance(item, tuple):
                continue
            sent_at = _header_date(item[1])
            if sent_at:
                counts[timezone.localtime(sent_at).date()] += 1
    return counts


def _header_date(raw_header: bytes):
    """Timezone-aware datetime from a fetched Date header, or None."""
    raw = email.message_from_bytes(raw_header).get("Date")
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else timezone.make_aware(parsed)


def _logout(imap) -> None:
    """Best-effort IMAP teardown; a failed logout must not mask the real work."""
    try:
        imap.close()
    except Exception:
        pass
    try:
        imap.logout()
    except Exception:
        pass
