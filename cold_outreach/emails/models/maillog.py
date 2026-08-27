# cold_outreach/emails/models/maillog.py
"""The mail log — what happened, kept apart from what it meant.

Every message our mailboxes emit or receive gets a row **before** anything
decides what it is, and every answer a receiving server gives about a send gets
its own row. That separation is the whole point of this module:

> the record is complete, factual and append-only; the interpretation is derived,
> versioned and recomputable.

While reading *was* deciding, a message the reader had no rule for left no trace:
*unread* and *nothing to read* were the same state, so nine days of lost mail
looked exactly like nine quiet days, and the only human reply an install ever
received is not in its CRM. Here the bytes land first, ``kind`` is written by a
pure pass over them (``emails/classify.py``), and improving the rules means
bumping ``classifier_version`` and re-running over every message ever received —
history is repaired, not just future mail.

Nothing here knows about deals, campaigns or leads. A stranger cold-mailing the
box is a ``Thread`` with no deal: recorded, classified ``unrelated``, and not a
special case.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models


class Direction(models.TextChoices):
    INBOUND = "in", "Received"
    OUTBOUND = "out", "Sent"


class Kind(models.TextChoices):
    """What a message turned out to be. Blank until ``classify`` has run."""

    HUMAN_REPLY = "human_reply", "Human reply"
    BOUNCE = "bounce", "Non-delivery report"
    AUTO_REPLY = "auto_reply", "Automatic reply"
    OPT_OUT = "opt_out", "Opt-out"
    UNRELATED = "unrelated", "Unrelated"
    OUTBOUND = "outbound", "Sent by us"


# The kinds that are *turns in a conversation*. An NDR is a fact about delivery,
# never something anybody said, so it cannot reach ``chat_summary`` or the agent's
# context — closed here, structurally, rather than by a filter someone must
# remember to apply at each read site.
TURN_KINDS = (Kind.HUMAN_REPLY, Kind.OUTBOUND)


class Thread(models.Model):
    """One conversation, as mail clients define it: a graph over Message-IDs.

    A thread is not a root pointer. On insert a message inherits the thread of
    anything its ``References``/``In-Reply-To`` names, and two threads that turn
    out to be joined by a later message are merged — union-find over Message-IDs.
    Matching on the *root* alone (the opener's id, which is all ``Deal`` used to
    carry) drops any reply from a client that sends only ``In-Reply-To``, because
    that header points at the **latest** message in the chain and not at the root.

    A thread belongs to a mailbox, so a message quoting an id we sent from a
    *different* box is never folded into the wrong conversation.
    """

    mailbox = models.ForeignKey(
        "emails.Mailbox", on_delete=models.CASCADE, related_name="threads",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"thread {self.pk} on {self.mailbox_id}"

    def turns(self):
        """The conversation, oldest first — what somebody actually said.

        Bounces, auto-replies and unrelated mail are in the thread but are not
        turns: they are facts about the thread, not contributions to it.
        """
        return self.messages.filter(kind__in=TURN_KINDS).order_by("sent_at", "pk")


class Message(models.Model):
    """One RFC-5322 message one of our mailboxes emitted or received.

    Identity is ``(mailbox, message_id)`` — never the UID. UIDs are per-folder,
    die on a ``UIDVALIDITY`` bump, and on Gmail a "folder" is a label a message
    can lose; they are kept as *provenance*, so a row can say where it was seen
    without anything being allowed to treat that as who it is. Because identity
    is the Message-ID, the fetch cursor is only an optimisation: reset it to 0 and
    the next walk re-reads the box and stores nothing new.
    """

    mailbox = models.ForeignKey(
        "emails.Mailbox", on_delete=models.CASCADE, related_name="messages",
    )
    thread = models.ForeignKey(
        Thread, null=True, blank=True, on_delete=models.SET_NULL, related_name="messages",
    )
    direction = models.CharField(max_length=3, choices=Direction.choices)
    # Ours is minted at send time (``sender._mint_message_id``); theirs arrives in
    # the header. A message that carries none falls back to ``sha256:<digest>`` of
    # its raw bytes — a real identity rather than a guess, because we keep the
    # bytes that produced it.
    message_id = models.CharField(max_length=998)
    from_address = models.CharField(max_length=320, blank=True, default="")
    to_address = models.CharField(max_length=320, blank=True, default="")
    subject = models.CharField(max_length=998, blank=True, default="")
    # Threading headers, normalized once at insert: bare ids, no angle brackets,
    # so nothing downstream has to know that servers differ on the brackets.
    in_reply_to = models.CharField(max_length=998, blank=True, default="")
    references_ids = models.JSONField(default=list, blank=True)
    # Inbound: the bytes as fetched, subject to the retention rule in ``sync.py``
    # (a stranger's mail is recorded headers-only). Outbound: headers only — we
    # composed the body and it is already in ``body_text``.
    raw = models.BinaryField(null=True, blank=True)
    # Inbound: **derived** — the text/plain part with the quoted history stripped,
    # rewritten whenever the classifier re-runs. Outbound: **authoritative** — what
    # the agent composed, before the signature, opt-out and attribution lines,
    # which are identical on every send.
    body_text = models.TextField(blank=True, default="")
    # The message's own clock (its ``Date`` header, or the moment we sent it) and
    # ours. ``sent_at`` is what the conversation is ordered by; ``recorded_at`` is
    # the only timestamp nobody else can influence.
    sent_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)
    recorded_at = models.DateTimeField(auto_now_add=True)
    # Outbound openers only: which prompt line wrote this, and a hash of that line's
    # text at the time (``core/prompt_lines.py``). Both, because an edited line keeps
    # its id and stops being the same message — pooling the two under one name would
    # quietly corrupt any later comparison, and the rows cannot be re-attributed once
    # they are ambiguous. Blank on replies, and on an install that has no lines.
    #
    # **This is the whole A/B apparatus.** The line is drawn at random and written down
    # here; whether a send got an answer is already derivable from the thread. So the
    # comparison is a query rather than an experiment somebody had to design in
    # advance — and it only works from the first send onward, which is why the columns
    # exist before anything reads them.
    prompt_line_id = models.CharField(max_length=64, blank=True, default="")
    prompt_line_digest = models.CharField(max_length=64, blank=True, default="")
    # Provenance, never identity — see the class docstring.
    folder = models.CharField(max_length=255, blank=True, default="")
    uid = models.PositiveBigIntegerField(null=True, blank=True)
    uidvalidity = models.PositiveBigIntegerField(null=True, blank=True)
    # The interpretation, and which rules produced it. A row whose
    # ``classifier_version`` is behind the current one is simply re-read.
    kind = models.CharField(max_length=16, choices=Kind.choices, blank=True, default="")
    classified_at = models.DateTimeField(null=True, blank=True)
    classifier_version = models.PositiveSmallIntegerField(default=0)
    # NULL = **pending**: the message is stored and classified, and nothing has yet
    # acted on what it means (``emails/project.py``). This is the state that did not
    # exist, and whose absence let loss read as silence.
    processed_at = models.DateTimeField(null=True, blank=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="messages",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["mailbox", "message_id"], name="uniq_mailbox_message_id",
            ),
        ]
        indexes = [
            # The pending queues: classify walks one, project the other.
            models.Index(fields=["classifier_version", "classified_at"]),
            models.Index(fields=["processed_at"]),
            # The conversation read, and the send ledger.
            models.Index(fields=["thread", "sent_at"]),
            models.Index(fields=["mailbox", "direction", "sent_at"]),
        ]

    def __str__(self):
        return f"{self.get_direction_display()} {self.message_id}"

    @property
    def is_outbound(self) -> bool:
        return self.direction == Direction.OUTBOUND

    @property
    def is_pending(self) -> bool:
        """Stored, not yet acted on — the third state ``processed_at`` exists to name."""
        return self.processed_at is None


class DeliveryEvent(models.Model):
    """One thing the world said about one of our sends.

    A message has many of these over time, and that is the point: an SMTP ``250``
    at send time and a bounce four hours later are the same conversation with the
    receiver, and only both of them together make bounce rate arithmetic possible.

    The predecessor of this table was written **only on failure, from inside an
    exception handler** — so a hard bounce, which arrives as ordinary mail rather
    than as an exception, produced nothing at all: 590 sends, 0 rows, and a
    capacity ramp accelerating ×1.5/day into a failure it had no way to perceive.

    ``status`` is the fact. ``response`` is how ``delivery_policy`` reads it, kept
    beside the fact for the same reason ``Message.kind`` is — so the pausing rule
    is one indexed query rather than a re-derivation over every row.
    """

    class Status(models.TextChoices):
        ACCEPTED = "accepted", "Accepted by the receiver"
        DEFERRED = "deferred", "Deferred (4xx)"
        REJECTED = "rejected", "Rejected (5xx)"
        BOUNCED = "bounced", "Bounced (non-delivery report)"
        ERROR = "error", "No answer from the receiver"

    message = models.ForeignKey(
        Message, on_delete=models.CASCADE, related_name="delivery_events",
    )
    status = models.CharField(max_length=10, choices=Status.choices)
    # The reading of the event, from ``delivery_policy.Response``. Blank on an
    # ``accepted``, which is nobody objecting to anything.
    response = models.CharField(max_length=20, blank=True, default="")
    # The receiving server's own words: the SMTP code, the enhanced status
    # ("5.4.5" and "5.7.1" are both 550 and mean opposite things), and the text.
    smtp_code = models.PositiveSmallIntegerField(null=True, blank=True)
    enhanced_status = models.CharField(max_length=16, blank=True, default="")
    detail = models.CharField(max_length=255, blank=True, default="")
    # The ``250`` queue id — the receiver's own handle on this message, which is
    # what a provider asks for when a delivery is disputed. It was discarded.
    queue_id = models.CharField(max_length=128, blank=True, default="")
    # The inbound message that raised this event — set on a ``bounced``, so the
    # NDR that produced the verdict can always be read back.
    reported_by = models.ForeignKey(
        Message, null=True, blank=True, on_delete=models.SET_NULL, related_name="reports",
    )
    occurred_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["message", "occurred_at"])]

    def __str__(self):
        return f"{self.status} {self.smtp_code or ''} {self.enhanced_status}".strip()


class FolderCoverage(models.Model):
    """How much of one ``(mailbox, folder)`` we have actually read.

    A claim about *our knowledge*, and the only thing a step may read as one.
    ``last_uid`` is **assigned from what we stored**, never ``max()``-ed over what
    we saw — the incident this replaces was a cursor no correct pass could walk
    back down, so a changed meaning could only ever skip mail. ``synced_at`` moves
    only on a walk that reached the end, so "we are current" and "we got partway"
    are distinguishable without a human opening an IMAP session.

    None of it is correctness machinery: identity is the Message-ID, so resetting
    a row to zero costs one re-walk and stores nothing new.
    """

    mailbox = models.ForeignKey(
        "emails.Mailbox", on_delete=models.CASCADE, related_name="coverage",
    )
    folder = models.CharField(max_length=255)
    # The server's declared UID epoch. When it changes the server has reissued its
    # UIDs and the stored cursor points at unrelated mail, so the walk restarts.
    uidvalidity = models.PositiveBigIntegerField(default=0)
    last_uid = models.PositiveBigIntegerField(default=0)
    # When a walk last ran to completion. NULL after a walk that stopped early —
    # a message that cannot be fetched is not stepped over.
    synced_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Folder coverage"
        constraints = [
            models.UniqueConstraint(
                fields=["mailbox", "folder"], name="uniq_mailbox_folder",
            ),
        ]

    def __str__(self):
        return f"{self.mailbox_id}:{self.folder}@{self.last_uid}"
