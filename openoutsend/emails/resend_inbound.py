# openoutsend/emails/resend_inbound.py
"""**sync** for a resend box — the mail Resend Inbound received, polled into the log.

A resend box has no inbox to read. With ``OUTSEND_REPLY_TO`` set, every send names an
address on a domain whose MX is Resend's, and what arrives there waits in Resend's
received list. Each pass reads that list and stores what it has not stored, as the
bytes that arrived, so ``classify`` and ``project`` run on it exactly as they do on a
message fetched over IMAP (``emails/sync.py``).

**Everything received on the reply domain is kept.** IMAP sync keeps only mail it can
tie to a conversation, because a mailbox is a person's. The reply domain is nobody's
mailbox, so whatever lands on it was sent to the install — including answers to mail
this install did not send, which ``classify`` then calls ``unrelated``. Mail the
account received on other domains is not this box's and is left alone.

The same two rules as IMAP sync carry the correctness:

- **Identity is the Message-ID.** The list names each email's, so one already held is
  skipped without a download, and a re-poll stores nothing new.
- **A message that cannot be fetched is not stepped over.** The walk goes oldest
  first and stops in front of it, and the cursor does not move, so the next pass
  starts from the same place and tries it again.

The cursor is the coverage row's ``synced_at`` — the moment the last complete walk
began — read back with a margin for the two clocks. The UID columns stay 0: Resend has
no UIDs. A box's first walk reads the whole list, as far as ``resend.MAX_PAGES`` goes;
the reply domain has no history that is not ours.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from email.utils import getaddresses

from django.utils import timezone

from openoutsend.emails import parsing
from openoutsend.emails.models import FolderCoverage, Message

logger = logging.getLogger(__name__)

# The folder a received message and its coverage are recorded under — provenance only.
RECEIVED = "resend"
# How far behind the last walk's start a pass reads again. Resend stamps an email when
# it took it in, on its own clock; a re-read costs a lookup per message and nothing else.
CURSOR_MARGIN = timedelta(minutes=10)


def poll(mailbox) -> int:
    """Store what Resend received on the reply domain since the last walk. Returns rows stored.

    Nothing to do without ``OUTSEND_REPLY_TO``. Best-effort like an IMAP sync: an
    unreachable or refusing API logs and returns 0 with the cursor untouched.
    """
    from openoutsend.core.conf import reply_to
    from openoutsend.emails import resend

    address = reply_to()
    if not address:
        return 0
    coverage, _ = FolderCoverage.objects.get_or_create(mailbox=mailbox, folder=RECEIVED)
    started = timezone.now()
    since = coverage.synced_at - CURSOR_MARGIN if coverage.synced_at else None
    try:
        listed = resend.received_emails(mailbox.password, since=since)
    except (resend.ResendError, OSError) as exc:
        logger.warning("resend: could not read what %s received (%s)",
                       address.rpartition("@")[2], exc)
        return 0

    stored, complete = _walk(mailbox, address.rpartition("@")[2], reversed(listed))
    if complete:
        coverage.synced_at = started
        coverage.save(update_fields=["synced_at", "updated_at"])
    if stored:
        logger.info("resend: %s stored %d received message(s)", mailbox.from_address, stored)
    return stored


def _walk(mailbox, domain: str, listed) -> tuple[int, bool]:
    """Store each listed email in turn, oldest first. Returns ``(rows stored, reached the end)``."""
    from openoutsend.emails import resend

    stored = 0
    for item in listed:
        if not _received_on(item, domain) or _held(mailbox, item):
            continue
        try:
            raw = resend.received_raw(mailbox.password, item["id"])
        except (resend.ResendError, OSError) as exc:
            logger.warning("resend: received email %s could not be fetched (%s) — "
                           "stopping before it", item["id"], exc)
            return stored, False
        if raw is None:
            logger.warning("resend: received email %s has no original file — "
                           "stopping before it", item["id"])
            return stored, False
        stored += _store(mailbox, raw)
    return stored, True


def _received_on(item: dict, domain: str) -> bool:
    """True when the email was addressed to anyone on *domain*."""
    recipients = [*(item.get("to") or []), *(item.get("cc") or [])]
    return any(address.lower().endswith(f"@{domain}")
               for _, address in getaddresses(recipients))


def _held(mailbox, item: dict) -> bool:
    """True when the list's Message-ID is one this box already stores.

    A list entry with no Message-ID is never held by this test — its identity is a
    digest of its bytes, so it is downloaded, and ``_store`` decides.
    """
    message_id = parsing.normalize_id(item.get("message_id") or "")
    return bool(message_id) and Message.objects.filter(
        mailbox=mailbox, message_id=message_id).exists()


def _store(mailbox, raw: bytes) -> bool:
    """Record one received message. True when stored, False when already held."""
    from openoutsend.emails.sync import record

    msg = parsing.parse(raw)
    message_id = parsing.message_id_of(msg, raw)
    if Message.objects.filter(mailbox=mailbox, message_id=message_id).exists():
        return False
    record(mailbox, msg, message_id, raw, folder=RECEIVED)
    return True
