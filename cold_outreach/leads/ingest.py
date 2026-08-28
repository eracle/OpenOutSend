"""Ingest — JSON Lines on stdin become rows, and the door closes behind them.

The receiving end of `openoutreach find --json | outsend`. It reads, upserts, and
exits; it transmits nothing. Every rule here is the boundary contract's, and each one
exists because the pipe is **at-least-once into an idempotent receiver**: recovery is
running it again, so nothing may depend on a line arriving exactly once.

- **Keyed on `(lead_id, campaign)`.** A re-ingest lands on the row it landed on last
  time, or re-running the pipe is a way of mailing people twice.
- **Latest wins, field by field** — a re-ingest is a *correction*: a lead re-qualified
  under a changed ICP has a new `reason`, and an address filled in by a later
  enrichment beats the blank it replaces. An empty value never overwrites a stored
  one, because "we were never told" is not a correction of anything.
- **Suppression is checked at the door and is terminal.** An opted-out address is
  stored — we still know who they are — and its deal is parked `UNSUBSCRIBED`, which
  no later ingest lifts. A blank address has nothing to check, which is why
  `emails/sender.suppressed` asks again at send.
- **An address that *changed* is re-checked**, because ingest is lead-keyed while
  suppression is address-keyed: a corrected address is an unsuppressed one until
  something looks again.
- **A malformed line is skipped and counted**, named on stderr, and makes the exit
  non-zero. `find` spent real money discovering the rows behind it, so aborting the
  batch throws away paid work — and a silent skip would be worse than either, because
  a producer emitting garbage has to be findable.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import IO

from django.utils.dateparse import parse_datetime

from cold_outreach.leads.models import Deal, DealState, Lead, Outcome
from cold_outreach.leads.suppression import is_suppressed, normalize

logger = logging.getLogger(__name__)

# The record's own field names, minus the two that are not the lead's own: `reason`
# and `qualified_at` are per-campaign and live on the Deal.
LEAD_FIELDS = (
    "email",
    "first_name",
    "last_name",
    "company",
    "title",
    "website",
    "linkedin_url",
    "profile_text",
)


@dataclass
class IngestResult:
    """What one run of the pipe did. Every number here is printed to stderr."""

    stored: int = 0
    suppressed: int = 0
    skipped: int = 0

    @property
    def ok(self) -> bool:
        """True when every line that arrived became a row."""
        return self.skipped == 0


def ingest(stream: IO[str], campaign) -> IngestResult:
    """Read JSON Lines from *stream* into *campaign*. Returns what happened.

    Line by line, so a stream that stops halfway has already delivered every complete
    record before the break — which is the property JSON Lines was chosen for, and the
    reason the rest is simply a re-run.
    """
    result = IngestResult()
    for number, line in enumerate(stream, start=1):
        line = line.strip()
        if not line:
            continue
        record = _parse(line, number)
        if record is None:
            result.skipped += 1
            continue
        _store(record, campaign, result)
    return result


def _parse(line: str, number: int) -> dict | None:
    """One line as a record, or None with the reason named on stderr.

    A record without a `lead_id` is malformed rather than merely odd: the key is what
    makes a re-ingest idempotent, and a row that cannot be keyed would be a duplicate
    every time the pipe ran.

    Unknown keys are kept, not rejected — the compatibility rule is that a receiver
    ignores what it does not know and the finder only ever adds, so a record from a
    newer producer has to land here intact.
    """
    try:
        record = json.loads(line)
    except json.JSONDecodeError as exc:
        logger.error("line %d skipped: not JSON (%s)", number, exc.msg)
        return None
    if not isinstance(record, dict):
        logger.error("line %d skipped: not a JSON object", number)
        return None
    if not str(record.get("lead_id") or "").strip():
        logger.error("line %d skipped: no lead_id", number)
        return None
    return record


def _store(record: dict, campaign, result: IngestResult) -> None:
    """Upsert one record's lead and deal, and count what it turned out to be."""
    lead, address_changed = _upsert_lead(record)
    suppressed = _check_the_door(lead, address_changed)
    _upsert_deal(lead, campaign, record, suppressed)

    result.stored += 1
    if suppressed:
        result.suppressed += 1


def _upsert_lead(record: dict) -> tuple[Lead, bool]:
    """The lead this record is about, created or corrected. Says whether its address moved.

    Only non-empty values are written. The record carries `null` for a field the
    producer was never told about — a name it could not resolve, an address that has
    not been bought yet — and letting that overwrite a value we already hold would
    turn every later run into data loss.
    """
    lead, created = Lead.objects.get_or_create(lead_id=str(record["lead_id"]).strip())

    incoming = {
        field: value.strip()
        for field in LEAD_FIELDS
        if isinstance(value := record.get(field), str) and value.strip()
    }
    if "email" in incoming:
        incoming["email"] = normalize(incoming["email"])

    changed = [field for field, value in incoming.items() if getattr(lead, field) != value]
    address_changed = "email" in changed and not created
    for field in changed:
        setattr(lead, field, incoming[field])
    if changed or created:
        lead.save()
    return lead, address_changed


def _check_the_door(lead: Lead, address_changed: bool) -> bool:
    """Is this person on the suppression list? Log the re-check when the address moved.

    The re-check is not a separate query — the door is asked about every row anyway —
    but it is worth a line when it fires, because a corrected address walking somebody
    back into the sendable set is the exact failure this rule exists to prevent, and
    silence would make the save look like nothing happened.
    """
    suppressed = is_suppressed(lead.email)
    if address_changed and suppressed:
        logger.warning("%s changed address into a suppressed one — staying suppressed", lead.public_id)
    return suppressed


def _upsert_deal(lead: Lead, campaign, record: dict, suppressed: bool) -> Deal:
    """This lead's row under this campaign, created or corrected.

    `reason` is refreshed on every arrival — that is the correction a re-ingest most
    often *is*. The state is not: a conversation that has started or ended is this
    side's own business, and a producer re-printing its whole campaign every night
    must never reset it. The one state ingest sets is the terminal one, and only
    downwards — and a deal that already reached it keeps the outcome it reached it
    with, exactly as `suppress_email` leaves one alone. Ending is not sendable, so
    there is nothing to enforce and nothing to overwrite.
    """
    opening = {"state": DealState.READY}
    if suppressed:
        opening = {"state": DealState.COMPLETED, "outcome": Outcome.UNSUBSCRIBED}
    deal, created = Deal.objects.get_or_create(
        lead=lead, campaign=campaign, defaults=opening,
    )

    updated = []
    reason = (record.get("reason") or "").strip()
    if reason and deal.reason != reason:
        deal.reason = reason
        updated.append("reason")
    qualified_at = parse_datetime(str(record.get("qualified_at") or "")) if record.get("qualified_at") else None
    if qualified_at and deal.qualified_at != qualified_at:
        deal.qualified_at = qualified_at
        updated.append("qualified_at")
    if suppressed and not created and deal.state != DealState.COMPLETED:
        deal.state = DealState.COMPLETED
        deal.outcome = Outcome.UNSUBSCRIBED
        updated += ["state", "outcome"]

    if updated:
        deal.save(update_fields=[*updated, "updated_at"])
    return deal
