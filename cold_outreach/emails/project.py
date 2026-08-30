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

**A bounce that names a dead address ends the pursuit.** Only the receiver's own
explicit statement counts — the enhanced statuses in
``delivery_policy._DEAD_ADDRESS_STATUSES``, never the 5.7.x policy class, which is
about this box rather than the recipient. Everything else is recorded and left
alone, so a deferral and a full mailbox stay sendable. Whether a dead address may
later be *replaced* is untouched: suppression is keyed on the address, so a new one
found for the same person is sendable the moment it arrives.
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
    """Raise a ``bounced`` event against the send this report is about, and stop
    mailing the address when the receiver says there is nobody there.

    An NDR whose original message cannot be identified still leaves its own row
    and is still counted as a bounce for the box; it just has no send to attach
    to. Losing the link is a gap in the arithmetic, not a reason to drop the fact.

    **Suppression hangs off the identified send, never off the report.** The address
    to stop mailing is the one *we wrote to*, which only the original message knows;
    a report arrives from a mailer-daemon, so suppressing its sender would put a
    postmaster on the list and leave the dead address live.
    """
    from cold_outreach.emails.delivery_policy import Response, address_is_dead, stop_mailing

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

    if address_is_dead(status):
        ended = stop_mailing(original.to_address, status)
        logger.warning("project: %s is undeliverable (%s) — %d open deal(s) ended",
                       original.to_address, status, ended)


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
    """Suppress the address this opt-out came from, for good.

    Enforcement is address-level and terminal: the list outlives the deals it ends,
    so no later ingest can resurrect somebody who asked to stop.
    """
    from cold_outreach.leads.suppression import suppress_email

    if not message.from_address:
        return
    ended = suppress_email(message.from_address, reason="opt-out to the unsubscribe alias")
    logger.info("project: opt-out from %s — %d open deal(s) ended",
                message.from_address, ended)
