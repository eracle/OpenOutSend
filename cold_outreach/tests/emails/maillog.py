# tests/emails/maillog.py
"""Builders for mail-log rows — what a test needs to stand a conversation up.

Kept out of the fake IMAP server on purpose: most tests about the log never go
near a mailbox, because the log is exactly the thing that no longer needs one.
"""
from __future__ import annotations

from django.utils import timezone

from cold_outreach.emails.models import (
    DeliveryEvent,
    Direction,
    FolderCoverage,
    Kind,
    Mailbox,
    Message,
    Thread,
)


def mailbox(address: str = "sender@corp.com", *, watching: bool = True, **kwargs) -> Mailbox:
    """A connected mailbox.

    ``watching`` gives it INBOX coverage at UID 0 — a box that was connected
    *before* the mail a test is about arrived. Without it the first pass starts at
    the folder's high-water mark instead, which is the real first-sight rule (a
    connected Gmail box is not mirrored back through its history) and is tested on
    its own in ``test_sync.py``.
    """
    box = Mailbox.objects.create(
        username=address, password="pw", from_address=address, **kwargs,
    )
    if watching:
        FolderCoverage.objects.create(mailbox=box, folder="INBOX", uidvalidity=1)
    return box


def outbound(box, *, thread=None, to="lead@acme.com", message_id=None,
             body="Hi there", sent_at=None, subject="Quick question") -> Message:
    """One send, as ``sender.py`` writes it: classified, processed, in a thread."""
    now = sent_at or timezone.now()
    thread = thread or Thread.objects.create(mailbox=box)
    return Message.objects.create(
        mailbox=box,
        thread=thread,
        direction=Direction.OUTBOUND,
        message_id=message_id or f"out-{Message.objects.count()}@corp.com",
        from_address=box.from_address,
        to_address=to,
        subject=subject,
        body_text=body,
        sent_at=now,
        kind=Kind.OUTBOUND,
        classified_at=now,
        classifier_version=1,
        processed_at=now,
    )


def inbound(box, *, thread=None, sender="lead@acme.com", message_id=None,
            body="Sure, happy to chat.", kind=Kind.HUMAN_REPLY, sent_at=None,
            in_reply_to="", references=None, processed=True) -> Message:
    """One received message, already classified — the state after a mail pass."""
    now = sent_at or timezone.now()
    return Message.objects.create(
        mailbox=box,
        thread=thread,
        direction=Direction.INBOUND,
        message_id=message_id or f"in-{Message.objects.count()}@acme.com",
        from_address=sender,
        to_address=box.from_address,
        body_text=body,
        sent_at=now,
        received_at=now,
        in_reply_to=in_reply_to,
        references_ids=references or [],
        kind=kind,
        classified_at=now,
        classifier_version=1,
        processed_at=now if processed else None,
    )


def accepted(message) -> DeliveryEvent:
    return DeliveryEvent.objects.create(
        message=message, status=DeliveryEvent.Status.ACCEPTED, smtp_code=250,
    )


def bounced(message, *, response="refused") -> DeliveryEvent:
    return DeliveryEvent.objects.create(
        message=message, status=DeliveryEvent.Status.BOUNCED, response=response,
        enhanced_status="5.1.1",
    )
