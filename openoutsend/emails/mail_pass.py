# openoutsend/emails/mail_pass.py
"""The mail pass: sync, then classify, then project — in that order, every time.

The order is the design. While one pass read and decided at once, a message that
matched no rule left no row anywhere, so *unread* and *nothing to read* were the
same state. Splitting it means the bytes land before anything has an opinion:

    sync      IMAP, or Resend Inbound → Message rows    (the only network step)
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
    """One full pass over every mailbox. Returns ``(mirrored, classified, projected)``.

    A resend box has no mailbox to read. Its sync is the poll of what Resend received
    on the reply domain (``emails/resend_inbound.py``), counted as mirrored; beside it,
    the poll of Resend's email list for bounces and complaints
    (``emails/resend_events.py``) is counted as projected — it writes the same events
    and suppressions a projected bounce does.
    """
    from openoutsend.emails import resend_events, resend_inbound
    from openoutsend.emails.classify import classify_pending
    from openoutsend.emails.models import Mailbox
    from openoutsend.emails.project import project_pending
    from openoutsend.emails.sync import mirror

    boxes = list(Mailbox.objects.all())
    mailboxes = [box for box in boxes if not box.is_resend]
    resend_boxes = [box for box in boxes if box.is_resend]
    logger.info("mail pass: reading %d mailbox(es), polling %d resend box(es)",
                len(mailboxes), len(resend_boxes))
    mirrored = (sum(mirror(box) for box in mailboxes)
                + sum(resend_inbound.poll(box) for box in resend_boxes))
    polled = sum(resend_events.poll(box) for box in resend_boxes)
    return mirrored, classify_pending(), project_pending() + polled
