# cold_outreach/emails/threads.py
"""Threading — union-find over Message-IDs, which is how every mail client does it.

A conversation is a graph, not a root pointer. Each message names the ids it is
answering; a message inherits the thread of anything it names, and when it names
two threads at once — the halves of a chain that arrived out of order — those
threads are merged into one.

The rule this replaces matched the **root** only (``Deal.email_message_id``, the
opener's Message-ID). That works while the client on the other end sends
``References``, which carries the whole chain. A client that sends only
``In-Reply-To`` points at the *latest* message instead, matches nothing, and its
reply is dropped — every id needed to place it was already stored on the outgoing
rows and simply never queried.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.db.models import Q

from cold_outreach.emails.models import Message, Thread

logger = logging.getLogger(__name__)


@transaction.atomic
def assign(message: Message) -> Thread:
    """Put *message* in its thread, merging any threads it joins. Returns the thread.

    Idempotent: running it again over the same message reaches the same thread, so
    re-classification repairs threading instead of fragmenting it.
    """
    threads = _threads_of(_relatives(message))
    if message.thread_id:
        threads.append(message.thread_id)

    if not threads:
        thread = Thread.objects.create(mailbox=message.mailbox)
    else:
        # Oldest wins, so a merge never renames the thread a deal already points at.
        keeper, *rest = sorted(set(threads))
        thread = Thread.objects.get(pk=keeper)
        for other in rest:
            _merge(other, thread)

    if message.thread_id != thread.pk:
        message.thread = thread
        message.save(update_fields=["thread"])
    return thread


def _relatives(message: Message):
    """Messages in the same box that this one answers, or that answer it.

    Both directions, because mail arrives out of order: the reply to a message we
    have not yet fetched is as common as the other way round. The backward edge is
    matched on ``in_reply_to`` alone — a client that fills ``References`` fills
    ``In-Reply-To`` too, and a JSON containment lookup is not available on SQLite.
    """
    named = set(message.references_ids or [])
    if message.in_reply_to:
        named.add(message.in_reply_to)

    lookup = Q(in_reply_to=message.message_id)
    if named:
        lookup |= Q(message_id__in=named)
    return (
        Message.objects.filter(mailbox_id=message.mailbox_id)
        .filter(lookup)
        .exclude(pk=message.pk)
    )


def _threads_of(messages) -> list[int]:
    """The thread ids among *messages*, ignoring any not yet threaded."""
    return [pk for pk in messages.values_list("thread_id", flat=True) if pk]


def _merge(source_id: int, target: Thread) -> None:
    """Move everything on thread *source_id* onto *target*, then drop the empty thread.

    Deals move first: a thread is deleted only once nothing points at it, so a
    merge can never null a deal's conversation on its way past.
    """
    from openoutreach.crm.models import Deal

    Deal.objects.filter(thread_id=source_id).update(thread=target)
    moved = Message.objects.filter(thread_id=source_id).update(thread=target)
    Thread.objects.filter(pk=source_id).delete()
    logger.info("threads: merged %d into %d (%d message(s))", source_id, target.pk, moved)
