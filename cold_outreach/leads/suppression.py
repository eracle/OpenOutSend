"""The suppression list — the door, and the last gate before a message is built.

Two writers reach it: the mail pass, when an opt-out lands in the box
(`emails/project.py`, `emails/steps/reply.py`), and ingest, which checks it on every
row that arrives. One reader matters more than either — `emails/sender.suppressed`,
which asks again at send time, because an unsubscribe can land in the seconds an LLM
call takes.

Addresses are normalised on the way in and on the way out, so a capitalised reply
address and a lowercased export row are the same person here.
"""
from __future__ import annotations

import logging

from cold_outreach.leads.models import Deal, DealState, Suppression

logger = logging.getLogger(__name__)


def normalize(address: str | None) -> str:
    """The comparable form of an email address: stripped and lowercased."""
    return (address or "").strip().lower()


def is_suppressed(address: str | None) -> bool:
    """True when *address* is on the list. Blank is never suppressed — it is unknown.

    A blank address has nothing to check, which is why the door cannot be the only
    gate: a row ingested without one is stored, and the check that matters for it
    happens at send, once an enrichment has filled the address in.
    """
    email = normalize(address)
    return bool(email) and Suppression.objects.filter(email=email).exists()


def suppress_email(address: str | None, *, reason: str = "") -> int:
    """Put *address* on the list for good and end its open conversations.

    Returns the number of deals moved to `UNSUBSCRIBED` — the deals rather than the
    leads, because the request is to stop being emailed and a deal is what would have
    emailed them. Already-terminal deals are left where they are: an opt-out arriving
    after a conversation completed changes nothing about how it ended.

    Idempotent, and the first suppression wins. Re-suppressing keeps the original
    timestamp and reason, since when somebody *first* asked is the fact worth having.
    """
    email = normalize(address)
    if not email:
        return 0

    _, created = Suppression.objects.get_or_create(email=email, defaults={"reason": reason})
    # ``iexact``, because the address on a lead is whatever its producer wrote. Ingest
    # normalises on the way in, but the duty is to the person, not to a casing, and a
    # row that arrived by any other route must not slip through.
    ended = (
        Deal.objects.filter(lead__email__iexact=email)
        .exclude(state__in=(DealState.COMPLETED, DealState.UNSUBSCRIBED))
        .update(state=DealState.UNSUBSCRIBED)
    )
    logger.info(
        "suppressed %s (%s) — %d open deal(s) ended",
        email, reason or "no reason given", ended,
    )
    if not created:
        logger.debug("%s was already suppressed", email)
    return ended
