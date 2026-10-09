# tests/emails/fake_imap.py
"""A minimal IMAP server for the sync job — one folder of whole RFC-822 messages.

Only what ``sync.mirror`` calls: ``folder_status`` for the UID epoch,
``select_folder``, a ``search`` honouring the ``UID lo:*`` range, and a ``fetch``
answering either ``BODY.PEEK[HEADER]`` or ``BODY.PEEK[]``. Fetches are recorded so
a test can assert the walk did *not* pull the body of a stranger's mail, and any
UID can be made unreadable so the "stop, don't step over it" rule is testable.
"""
from __future__ import annotations

from datetime import datetime, timezone

from imapclient.exceptions import IMAPClientError

# The ``Date`` header every fake message carries. Fixed rather than "now" so a
# test that cares about ordering can place our own sends around it explicitly.
RECEIVED_AT = datetime(2026, 3, 16, 10, 0, tzinfo=timezone.utc)

DAEMON_BOUNCE_BODY = (
    "Your message could not be delivered.\r\n"
    "\r\n"
    "Final-Recipient: rfc822; {recipient}\r\n"
    "{status_line}"
    "\r\n"
    "----- Original message -----\r\n"
    "Message-ID: {original}\r\n"
)


def message(uid, *, to, sender, subject="Re: Hi", body="Sure, happy to chat.",
            message_id=None, references=None, in_reply_to=None, headers=()):
    """One inbox message, as ``(uid, raw bytes)``."""
    lines = [
        f"From: {sender}",
        f"To: {to}",
        f"Subject: {subject}",
        f"Message-ID: {message_id or f'<m{uid}@corp.com>'}",
        "Date: Mon, 16 Mar 2026 10:00:00 +0000",
        "Content-Type: text/plain; charset=utf-8",
    ]
    if references:
        lines.append(f"References: {references}")
    if in_reply_to:
        lines.append(f"In-Reply-To: {in_reply_to}")
    lines.extend(headers)
    return uid, ("\r\n".join(lines) + "\r\n\r\n" + body).encode()


def bounce(uid, *, to, original, recipient="p@corp.com", status="5.1.1"):
    """A non-delivery report from the receiving side's daemon.

    ``status`` is the enhanced status in the delivery-status part. ``None`` omits the
    line altogether, which is the shape of the many real reports that say a message
    failed without ever saying so in a form a machine can read.
    """
    return message(
        uid,
        to=to,
        sender="mailer-daemon@googlemail.com",
        subject="Delivery Status Notification (Failure)",
        message_id=f"<ndr{uid}@googlemail.com>",
        in_reply_to=original,
        body=DAEMON_BOUNCE_BODY.format(
            recipient=recipient,
            original=original,
            status_line=f"Status: {status}\r\n" if status else "",
        ),
    )


def auto_reply(uid, *, to, sender, original):
    """A vacation responder: threads like a reply, and nobody wrote it."""
    return message(
        uid, to=to, sender=sender, subject="Automatic reply: Hi",
        in_reply_to=original, body="I am out of the office until Monday.",
        headers=("Auto-Submitted: auto-replied",),
    )


class FakeIMAP:
    """An ``IMAPClient`` stand-in holding one folder."""

    def __init__(self, messages, uidvalidity=1, unreadable=()):
        self.messages = dict(messages)          # {uid: raw}
        self.uidvalidity = uidvalidity
        # UIDs the server refuses to hand over — a transient FETCH failure.
        self.unreadable = set(unreadable)
        self.searched = []
        self.body_fetches = []
        self.selected = None

    # ── context manager ───────────────────────────────────────────

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    # ── the calls sync makes ──────────────────────────────────────

    def login(self, username, password):
        return b"OK"

    def folder_status(self, folder, attributes):
        return {
            b"UIDVALIDITY": self.uidvalidity,
            b"UIDNEXT": max(self.messages, default=0) + 1,
        }

    def select_folder(self, folder, readonly=False):
        self.selected = folder
        return {b"EXISTS": len(self.messages)}

    def search(self, criteria):
        uid_range = criteria[-1]
        self.searched.append(uid_range)
        low = int(uid_range.split(":")[0])
        return sorted(uid for uid in self.messages if uid >= low)

    def fetch(self, uids, specs):
        uid, spec = int(uids[0]), specs[0]
        if uid in self.unreadable:
            raise IMAPClientError(f"UID {uid} is unreadable")
        raw = self.messages.get(uid)
        if raw is None:
            return {}
        if "HEADER" in spec:
            return {uid: {b"BODY[HEADER]": raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"}}
        self.body_fetches.append(uid)
        return {uid: {b"BODY[]": raw}}

    def logout(self):
        return b"OK"
