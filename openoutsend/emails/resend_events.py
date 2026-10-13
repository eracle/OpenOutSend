# openoutsend/emails/resend_events.py
"""What became of a resend box's sends — the bounces and complaints, polled back.

A mailbox learns about a bounce when the report lands in its INBOX. A resend box has
no inbox, and Resend's relay takes every message it is handed, so the receiver's
verdict only exists on Resend's side: each email in its list carries a ``last_event``.
This reads that list once per pass and records what it says against our own rows,
matched on the id the send was accepted under (``DeliveryEvent.queue_id``).

    bounced, suppressed   the address is undeliverable → suppressed, deals ended
    complained            the reader marked it spam    → suppressed, deals ended

``suppressed`` is Resend declining to send to an address already on its own list —
an earlier bounce or complaint on this account — so it means what a bounce means.

**Only the latest event is visible.** The list says what happened last, not
everything that happened; an email opened after it was delivered says ``opened``.
Bounces and complaints are what this is for, and both end the story, so they are the
ones a latest-only view does not lose.

**The window is a week.** A bounce arrives in minutes and a complaint in days; past a
week, a send is not polled again, and nothing it says afterwards is read.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

POLL_WINDOW_DAYS = 7


def poll(mailbox) -> int:
    """Record the bounces and complaints Resend reports for *mailbox*'s recent sends.

    Returns the events recorded. Best-effort like an IMAP sync: an unreachable or
    refusing API logs and returns 0, and the next pass asks again.
    """
    from openoutsend.emails import resend

    pending = _pending_sends(mailbox)
    if not pending:
        return 0
    since = min(event.occurred_at for event in pending.values()) - timedelta(minutes=5)
    try:
        emails = resend.recent_emails(mailbox.password, since=since)
    except (resend.ResendError, OSError) as exc:
        logger.warning("resend: could not read the email list for %s (%s)",
                       mailbox.from_address, exc)
        return 0

    recorded = 0
    for item in emails:
        accepted = pending.get(item.get("id") or "")
        status = _status_for(item.get("last_event") or "")
        if accepted is None or status is None:
            continue
        _record(accepted.message, status, item["last_event"])
        recorded += 1
    return recorded


def _pending_sends(mailbox) -> dict:
    """Resend id → acceptance, for this box's sends inside the window still unresolved.

    A send that already bounced or drew a complaint is settled and not asked about
    again — which is also what keeps a re-poll from recording one event twice.
    """
    from openoutsend.emails.models import DeliveryEvent

    settled = (DeliveryEvent.Status.BOUNCED, DeliveryEvent.Status.COMPLAINED)
    accepted = (
        DeliveryEvent.objects
        .filter(message__mailbox=mailbox, status=DeliveryEvent.Status.ACCEPTED,
                occurred_at__gte=timezone.now() - timedelta(days=POLL_WINDOW_DAYS))
        .exclude(queue_id="")
        .exclude(message__delivery_events__status__in=settled)
        .select_related("message")
    )
    return {event.queue_id: event for event in accepted}


def _status_for(last_event: str):
    """The ``DeliveryEvent`` status a Resend ``last_event`` records, or None for the rest."""
    from openoutsend.emails.models import DeliveryEvent

    return {
        "bounced": DeliveryEvent.Status.BOUNCED,
        "suppressed": DeliveryEvent.Status.BOUNCED,
        "complained": DeliveryEvent.Status.COMPLAINED,
    }.get(last_event)


def _record(message, status: str, last_event: str) -> None:
    """Log the event against *message* and stop mailing its recipient.

    A bounce counts in the bounce rate like a report read from an inbox; both end
    every open deal at the address, a bounce as undeliverable and a complaint as an
    unsubscribe — somebody who calls it spam has asked to stop as plainly as anyone.
    """
    from openoutsend.emails.delivery_policy import Response
    from openoutsend.emails.models import DeliveryEvent
    from openoutsend.leads.models import Outcome
    from openoutsend.leads.suppression import suppress_email

    bounced = status == DeliveryEvent.Status.BOUNCED
    DeliveryEvent.objects.create(
        message=message,
        status=status,
        response=Response.REFUSED if bounced else "",
        detail=f"resend: {last_event}",
    )
    ended = suppress_email(
        message.to_address,
        reason=f"{last_event} (resend)",
        outcome=Outcome.UNDELIVERABLE if bounced else Outcome.UNSUBSCRIBED,
    )
    logger.warning("%s %s — %d open deal(s) ended", message.to_address, last_event, ended)
