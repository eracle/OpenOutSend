# openoutsend/emails/filing.py
"""**file** — put a message the headers could not place into a lead's conversation.

Threading places a reply by the ids it names, and a reply to mail this install did
not send names nothing it holds: ``classify`` calls it ``unrelated``, and it is
nobody's turn. Whoever sent that other mail knows whose answer it is; this side
does not, and is not asked to guess. ``outsend file`` is how it is told.

Filing moves the message, and whatever its own thread already holds, into the
thread of the lead's newest deal, then reads it again. In a thread we sent into it
is a ``human_reply`` by the ordinary rule (``classify._joins_a_thread_we_started``),
so ``outsend reply`` hands it over and a later re-classification keeps it there:
nothing is stored about the filing beyond where the message now sits.
"""
from __future__ import annotations

import logging

from django.db import transaction

from openoutsend.emails import classify, threads
from openoutsend.emails.models import Direction, Kind, Message
from openoutsend.errors import OutsendError

logger = logging.getLogger(__name__)


@transaction.atomic
def file_message(message_id: str, lead_id: str) -> Message:
    """File the ``unrelated`` inbound *message_id* into lead *lead_id*'s conversation.

    Refuses anything but ``unrelated`` mail — a bounce, an opt-out or an
    out-of-office has its own reading, and a reply already in a conversation is
    placed — and a lead with no thread on the message's mailbox, since there is
    nothing to answer in.
    """
    message = _inbound(message_id)
    if message.kind != Kind.UNRELATED:
        raise OutsendError(f"{message_id} is {message.kind or 'not yet classified'} — "
                           "only unrelated mail is filed")
    thread = _newest_thread(lead_id, message.mailbox_id)

    threads.join(message, thread)
    for moved in thread.messages.filter(direction=Direction.INBOUND, kind=Kind.UNRELATED):
        classify.classify(moved)
    logger.info("file: %s joined lead %s's conversation (thread %d)",
                message_id, lead_id, thread.pk)
    message.refresh_from_db()
    return message


def _inbound(message_id: str) -> Message:
    """The one received message stored under *message_id*."""
    from openoutsend.emails import parsing

    found = list(
        Message.objects.filter(message_id=parsing.normalize_id(message_id),
                               direction=Direction.INBOUND)
        .select_related("mailbox")[:2]
    )
    if not found:
        raise OutsendError(f"no received message {message_id} in the store")
    if len(found) > 1:
        raise OutsendError(f"{message_id} was received on more than one mailbox")
    return found[0]


def _newest_thread(lead_id: str, mailbox_id: int):
    """The thread of *lead_id*'s newest deal on this mailbox."""
    from openoutsend.leads.models import Deal

    deal = (
        Deal.objects
        .filter(lead__lead_id=lead_id, thread__mailbox=mailbox_id)
        .select_related("thread")
        .order_by("-created_at", "-pk")
        .first()
    )
    if deal is None:
        raise OutsendError(f"lead {lead_id} has no conversation on this mailbox yet")
    return deal.thread
