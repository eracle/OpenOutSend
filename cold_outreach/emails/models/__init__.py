# cold_outreach/emails/models/__init__.py
"""The email schema, in two layers.

``mailbox.py`` is the sending identity. ``maillog.py`` is the **mail log**: one
row per message this install emitted or received, and one row per thing a
receiving server said about a send. The log is the record; ``kind`` and the
conversation projected from it are the interpretation, and they are derived from
rows that never move.
"""
from cold_outreach.emails.models.mailbox import Mailbox, MailboxManager, has_mailbox
from cold_outreach.emails.models.maillog import (
    DeliveryEvent,
    Direction,
    FolderCoverage,
    Kind,
    Message,
    Thread,
)

__all__ = [
    "DeliveryEvent",
    "Direction",
    "FolderCoverage",
    "Kind",
    "Mailbox",
    "MailboxManager",
    "Message",
    "Thread",
    "has_mailbox",
]
