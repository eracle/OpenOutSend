# cold_outreach/emails/mail_pass.py
"""The mail pass: sync, then classify, then project — in that order, every time.

The order is the design. While one pass read and decided at once, a message that
matched no rule left no row anywhere, so *unread* and *nothing to read* were the
same state. Splitting it means the bytes land before anything has an opinion:

    sync      IMAP → Message rows                       (the only network step)
    classify  stored bytes + our tables → kind, thread  (pure, versioned)
    project   classified messages → events, suppression (no bytes, no network)

Each step is safe to re-run and safe to interrupt. A box that cannot be reached
stops at ``sync`` and leaves the other two working on what is already stored, so
an outage delays reading the mail rather than interpreting it.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def run_mail_pass() -> tuple[int, int, int]:
    """One full pass over every mailbox. Returns ``(mirrored, classified, projected)``."""
    from cold_outreach.emails.classify import classify_pending
    from cold_outreach.emails.models import Mailbox
    from cold_outreach.emails.project import project_pending
    from cold_outreach.emails.sync import mirror

    boxes = list(Mailbox.objects.all())
    logger.info("mail pass: reading %d mailbox(es)", len(boxes))
    mirrored = sum(mirror(box) for box in boxes)
    return mirrored, classify_pending(), project_pending()
