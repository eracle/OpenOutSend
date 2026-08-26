# cold_outreach/emails/project.py
"""**project** — act on what a classified message means. No network, no bytes.

The third job, and the only one allowed to change anything outside the log: a
bounce becomes a ``DeliveryEvent`` against the send it killed, an opt-out
suppresses the person who asked. Everything it writes is derived from rows that
never move, so a corrected classification simply clears ``processed_at`` and the
projection is redone.

A ``human_reply`` is projected by doing **nothing**: it is already a turn in its
thread, and the cycle finds the deal by comparing the thread's newest inbound turn
with its newest outbound one. There is no second copy of the conversation to keep
in step — which is the dual-write this design deleted.

Nothing here decides what a bounce *means for a deal*. Whether a dead address
ends the pursuit or sends it back for re-enrichment is policy, and it is owned by
``p1-e2-email-bounce-detection-suppression``. This card only makes the bounce
visible — which is the part that was missing while capacity ramped ×1.5/day
against 590 sends and zero recorded verdicts.
"""
from __future__ import annotations

import logging
import re

from django.utils import timezone

from cold_outreach.emails.models import DeliveryEvent, Direction, Kind, Message

logger = logging.getLogger(__name__)

# Any Message-ID appearing anywhere in an NDR's bytes. A report quotes the failed
# message's headers in its own body parts, so the id we sent is in there even when
# the outer envelope carries no threading headers at all.
_ANY_MESSAGE_ID = re.compile(rb"<[^<>@\s]+@[^<>@\s]+>")

# The enhanced status inside a delivery-status part ("Status: 5.1.1").
_DSN_STATUS = re.compile(rb"^Status:\s*([245]\.\d{1,3}\.\d{1,3})", re.IGNORECASE | re.MULTILINE)


def project_pending(*, limit: int | None = None) -> int:
    """Act on every classified message nothing has acted on yet. Returns rows handled."""
    pending = (
        Message.objects.filter(processed_at__isnull=True)
        .exclude(kind="")
        .select_related("mailbox")
        .order_by("pk")
    )
    if limit is not None:
        pending = pending[:limit]

    handled = 0
    for message in pending:
        project(message)
        handled += 1
    if handled:
        logger.info("project: handled %d message(s)", handled)
    return handled


def project(message: Message) -> None:
    """Act on one message and stamp it processed."""
    if message.kind == Kind.BOUNCE:
        _record_bounce(message)
    elif message.kind == Kind.OPT_OUT:
        _honour_opt_out(message)

    message.processed_at = timezone.now()
    message.save(update_fields=["processed_at"])


def _record_bounce(ndr: Message) -> None:
    """Raise a ``bounced`` event against the send this report is about.

    An NDR whose original message cannot be identified still leaves its own row
    and is still counted as a bounce for the box; it just has no send to attach
    to. Losing the link is a gap in the arithmetic, not a reason to drop the fact.
    """
    from cold_outreach.emails.delivery_policy import Response

    original = _bounced_send(ndr)
    status = _dsn_status(ndr)
    if original is None:
        logger.warning("project: bounce %s on %s names no send we know",
                       ndr.message_id, ndr.mailbox.from_address)
        return

    DeliveryEvent.objects.create(
        message=original,
        status=DeliveryEvent.Status.BOUNCED,
        # A bounce is the receiver refusing the message, and it counts as pushback
        # for exactly that reason: it is the asynchronous half of a 5xx, and the
        # capacity ramp has to be able to feel it.
        response=Response.REFUSED,
        enhanced_status=status,
        detail=(ndr.subject or "")[:255],
        reported_by=ndr,
    )
    logger.warning("project: %s bounced (%s) — reported by %s",
                   original.to_address, status or "no status", ndr.message_id)


def _bounced_send(ndr: Message) -> Message | None:
    """The outbound message an NDR is reporting on, or None.

    Looks at every id the report names — in its threading headers *and* anywhere
    in its bytes, since a report quotes the failed message's headers in a body
    part — and keeps the one that is a send from this box.
    """
    named = list(ndr.references_ids or [])
    if ndr.in_reply_to:
        named.append(ndr.in_reply_to)
    if ndr.raw:
        named.extend(
            found.decode("utf-8", "replace").strip("<>")
            for found in _ANY_MESSAGE_ID.findall(bytes(ndr.raw))
        )
    if not named:
        return None
    return (
        Message.objects
        .filter(mailbox_id=ndr.mailbox_id, direction=Direction.OUTBOUND, message_id__in=named)
        .order_by("-sent_at")
        .first()
    )


def _dsn_status(ndr: Message) -> str:
    """The enhanced status the report carries ("5.1.1"), or ""."""
    if not ndr.raw:
        return ""
    match = _DSN_STATUS.search(bytes(ndr.raw))
    return match.group(1).decode() if match else ""


def _honour_opt_out(message: Message) -> None:
    """Suppress everyone holding the address this opt-out came from.

    Enforcement is account-level (``Lead.disqualified``), so it reaches every
    campaign holding the address rather than just this thread's.
    """
    from openoutreach.core.db.leads import suppress_email

    if not message.from_address:
        return
    suppressed = suppress_email(message.from_address)
    logger.info("project: opt-out from %s — %d lead(s) suppressed",
                message.from_address, suppressed)
