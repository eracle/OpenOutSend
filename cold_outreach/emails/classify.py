# cold_outreach/emails/classify.py
"""**classify** — decide what a stored message is. Pure, versioned, no network.

Reads the bytes ``sync`` kept and our own tables, and writes ``kind``, the derived
``body_text`` and the thread. Nothing else. That is what makes it re-runnable:
improve a rule, bump ``CLASSIFIER_VERSION``, and every message ever received is
re-read — **history is repaired, not only future mail**.

While reading *was* deciding, a rule that declined to keep a message deleted it.
The rules below are therefore allowed to be wrong: everything ``sync`` kept is
stored whole, so a message this classifier calls ``unrelated`` today can be read
again tomorrow and recovered. Only the sync-level rule about *whose* mail we keep
is irreversible, which is why it is drawn around the conversation rather than
around anything the classifier believes.

Order matters. An NDR is auto-submitted and quotes our own headers, so it would
answer to three of these rules; it is asked about first, because an NDR that
reads as a reply is what put a live install in a two-turn apology loop with a
dead address.
"""
from __future__ import annotations

import logging
import re

from django.utils import timezone

from cold_outreach.emails import parsing, threads
from cold_outreach.emails.models import Direction, Kind, Message

logger = logging.getLogger(__name__)

# Bump this when a rule below changes. Every row behind it is re-read on the next
# pass, and a row whose verdict moves is marked pending again so the projection is
# redone from the corrected reading.
CLASSIFIER_VERSION = 1

# Where an NDR comes from. The local part is what is stable across providers —
# the domain is the receiver's, not a fixed one.
_DAEMON_SENDERS = ("mailer-daemon", "postmaster", "mail-daemon", "no-reply-daemon")

_BOUNCE_SUBJECTS = re.compile(
    r"(undeliverable|undelivered|delivery status notification|returned mail|"
    r"delivery has failed|mail delivery failed|failure notice|delivery incomplete)",
    re.IGNORECASE,
)

_AUTO_REPLY_SUBJECTS = re.compile(
    r"(out of (the )?office|automatic reply|auto[- ]?reply|autoresponse|abwesenheit)",
    re.IGNORECASE,
)


def classify_pending(*, limit: int | None = None) -> int:
    """Classify every message no current-version verdict covers. Returns rows read.

    That set is both the never-classified and the classified-by-an-older-rule, so
    a version bump and a fresh mailbox are the same pass.
    """
    pending = (
        Message.objects.filter(classifier_version__lt=CLASSIFIER_VERSION)
        .select_related("mailbox")
        .order_by("pk")
    )
    if limit is not None:
        pending = pending[:limit]

    read = 0
    for message in pending:
        classify(message)
        read += 1
    if read:
        logger.info("classify: read %d message(s) at version %d", read, CLASSIFIER_VERSION)
    return read


def classify(message: Message) -> str:
    """Re-read one stored message and persist the verdict. Returns its ``kind``.

    A verdict that *changes* clears ``processed_at``: the projection was made from
    a reading that no longer holds, so it is owed again. A verdict that agrees with
    the stored one leaves the projection alone.
    """
    msg = parsing.parse(bytes(message.raw)) if message.raw else None
    kind = _kind_of(message, msg)
    body = message.body_text if message.is_outbound else (
        parsing.body_text(msg) if msg else "")

    changed = kind != message.kind
    message.kind = kind
    message.body_text = body
    message.classified_at = timezone.now()
    message.classifier_version = CLASSIFIER_VERSION
    fields = ["kind", "body_text", "classified_at", "classifier_version"]
    if changed and message.processed_at is not None:
        message.processed_at = None
        fields.append("processed_at")
        logger.info("classify: %s re-read as %s — projection owed again",
                    message.message_id, kind)
    message.save(update_fields=fields)

    if msg is not None:
        threads.assign(message)
    return kind


def _kind_of(message: Message, msg) -> str:
    """The rules, in the order they must be asked."""
    if message.direction == Direction.OUTBOUND:
        return Kind.OUTBOUND
    if msg is None:
        # No bytes, nothing to re-read. Rows backfilled from the conversation this
        # log replaced are the only ones in this state, and their verdict was made
        # by the reader that wrote them; overwriting a true verdict with a guess is
        # the one way a re-run could destroy history instead of repairing it.
        return message.kind or Kind.UNRELATED
    if is_bounce(msg):
        return Kind.BOUNCE
    if _is_opt_out(message, msg):
        return Kind.OPT_OUT
    if _is_auto_reply(msg):
        return Kind.AUTO_REPLY
    if _joins_a_thread_we_started(message):
        return Kind.HUMAN_REPLY
    return Kind.UNRELATED


def is_bounce(msg) -> bool:
    """True when this is a non-delivery report rather than something someone wrote.

    Public because ``sync`` asks it too: a report comes from the receiver's daemon
    rather than from anyone we wrote to, so it would otherwise fall outside the
    conversation and never be stored — and a bounce nobody stores is the failure
    this whole design exists to end.

    Three independent signals, because providers agree on none of them: the RFC
    3462 report container, the daemon the report comes from, and the subject line
    the daemon writes. Any one is enough — a missed NDR is read as a human reply,
    which is the expensive direction of this error.
    """
    if msg.get_content_type() == "multipart/report":
        return "delivery-status" in (msg.get_param("report-type") or "").lower()
    if any(part.get_content_type() == "message/delivery-status" for part in msg.walk()):
        return True
    local = parsing.sender_address(msg).split("@", 1)[0]
    if local in _DAEMON_SENDERS:
        return True
    return bool(_BOUNCE_SUBJECTS.search(msg.get("Subject") or ""))


def _is_opt_out(message: Message, msg) -> bool:
    """True when this was addressed to the box's ``+unsub`` alias.

    A client's unsubscribe button mints a fresh message with no threading headers
    at all, so the alias is the only thing that identifies it. A *worded*
    unsubscribe threads normally, reaches the agent, and is honoured there.
    """
    from cold_outreach.emails.sender import unsubscribe_address

    return parsing.addressed_to(msg, unsubscribe_address(message.mailbox.from_address))


def _is_auto_reply(msg) -> bool:
    """True for a vacation responder or any other machine-written courtesy.

    Not a turn: nobody said it, so answering it would be answering a program.
    """
    auto_submitted = (msg.get("Auto-Submitted") or "").strip().lower()
    if auto_submitted and auto_submitted != "no":
        return True
    if msg.get("X-Autoreply") or msg.get("X-Autorespond"):
        return True
    if (msg.get("Precedence") or "").strip().lower() in {"auto_reply", "bulk", "junk"}:
        return True
    return bool(_AUTO_REPLY_SUBJECTS.search(msg.get("Subject") or ""))


def _joins_a_thread_we_started(message: Message) -> bool:
    """True when this message lands in a thread the box has sent into.

    The whole test — no root matching, no per-deal header search. Threading has
    already placed the message (``threads.assign``); a thread holding an outbound
    message is a conversation we opened, and anything arriving into it from
    somebody else is a reply.
    """
    if not message.thread_id:
        return False
    if message.from_address == (message.mailbox.from_address or "").lower():
        return False
    return Message.objects.filter(
        thread_id=message.thread_id, direction=Direction.OUTBOUND,
    ).exists()
