# cold_outreach/emails/models/mailbox.py
"""Mailbox: one SMTP sending inbox, imported from the provider's creds export."""
from __future__ import annotations

from datetime import timedelta

from django.db import models
from django.utils import timezone

from cold_outreach.core.conf import WARM_FLOOR_SENDS
from cold_outreach.core.sending_window import (
    next_window_open,
    operator_timezone,
    within_sending_window,
)


def _local_midnight():
    """Start of today where the operator is — the horizon every per-day ledger counts from.

    The operator's zone, not the server's UTC, because the day is now a *window*: an
    operator in New York has their UTC midnight at 19:00, an hour before the window
    shuts, so a UTC ledger would reset the daily cap inside the working day and hand
    each box a fresh allowance for its last hour.
    """
    return timezone.localtime(timezone.now(), operator_timezone()).replace(
        hour=0, minute=0, second=0, microsecond=0)


class MailboxManager(models.Manager):
    """Pool-level send pacing — the daily-cap accounting the task and planner share."""

    def remaining_today(self) -> int:
        """Total sends left across the pool today (Σ per-box headroom).

        0 when no boxes exist or every box is at its cap.
        """
        return sum(box.headroom_today() for box in self.all())

    def free_for_first_email(self):
        """The box that may send a first email right now, or None.

        The clock speaks first and for the whole pool: outside the operator's
        working window no box may open a conversation, however much headroom it has
        (``core/sending_window.py``). Then three conditions, all per box: headroom
        left today, not paused by the receiver (both inside ``headroom_today``), and
        its spacing clock elapsed. Among the free boxes the most idle one wins, so
        volume spreads rather than piling on whichever row sorts first.

        Only *first* emails come through here. A reply is not cold volume and is
        sent regardless of window, cap or spacing. A **follow-up is** cold volume, but
        it cannot pick its box — the thread already has one — so it asks that box
        directly with ``Mailbox.free_now``.
        """
        if not within_sending_window():
            return None
        now = timezone.now()
        ranked = [
            (box, headroom) for box in self.all()
            if box.free_now(now) and (headroom := box.headroom_today()) > 0
        ]
        if not ranked:
            return None
        return max(ranked, key=lambda pair: pair[1])[0]

    def next_first_email_at(self, now=None):
        """When the pool could next open a conversation, or ``None`` if it never could.

        ``free_for_first_email`` says *not now* and cannot say *why* or *for how long* —
        which is all a bounded run needs to know before it decides to wait
        (``send_job.py``). The same three gates, read as a clock instead of a boolean:

            spacing   the box's own ``next_send_at``
            window    the next 08:00 at or after that
            cap       a box out of headroom is out until the ledger rolls, so it is
                      asked about tomorrow morning rather than about today

        The soonest box wins, because one free box is all a send needs. ``None`` means
        no mailbox is connected — the one state no amount of waiting resolves. A drained
        *lead* pool is not this method's business: it is about the boxes.
        """
        now = now or timezone.now()
        tomorrow = _local_midnight() + timedelta(days=1)
        return min(
            (
                next_window_open(max(now, box.next_send_at or now))
                if box.headroom_today() > 0 else next_window_open(tomorrow)
                for box in self.all()
            ),
            default=None,
        )

    def create_verified(
        self,
        *,
        from_address: str,
        password: str,
        host: str,
        port: int,
        imap_host: str,
        imap_port: int,
    ) -> tuple["Mailbox | None", str]:
        """Auth-check a mailbox over SMTP, then store it — the connect gate.

        The provider has no health API, so the SMTP login *is* the gate: nothing
        is stored unless auth succeeds, so a row always means working credentials.
        The SMTP username is the address itself (a mailbox you own logs in as
        itself; a distinct login is a relay case we don't support). Returns
        ``(mailbox, "")`` on success or ``(None, reason)`` when auth is rejected.
        Re-entering an address repairs that box in place (``update_or_create``).

        The new box keeps the floor capacity it is created with; the first
        ``refresh_capacity`` reads its Sent folder and replaces that with what the
        box has actually sustained. A box with real history is therefore throttled
        for at most one reconcile cycle, not for a warmup calendar.
        """
        from cold_outreach.emails.smtp import verify_auth

        ok, reason = verify_auth(host, port, from_address, password)
        if not ok:
            return None, reason
        box, _ = self.update_or_create(
            username=from_address,
            defaults={
                "password": password,
                "from_address": from_address,
                "host": host,
                "port": port,
                "imap_host": imap_host,
                "imap_port": imap_port,
            },
        )
        return box, ""


class Mailbox(models.Model):
    """One SMTP inbox, connected field-by-field at onboarding.

    A row exists only once its credentials pass the SMTP auth-check
    (``objects.create_verified``) — the provider has no health API, so the login
    is the gate. Send-time failures are not swallowed: a bad send fails its task
    and is retried, the box is left untouched (re-enter fixed credentials to
    repair it). host/port/imap default to IceMail's Google Workspace boxes; any
    other provider overrides them at entry.
    """

    host = models.CharField(max_length=255, default="smtp.gmail.com")
    port = models.PositiveIntegerField(default=587)
    # IMAP read side, for the agentic follow-up loop's reply-reader
    # (emails/sync.py); authenticates with the same address + app password as SMTP.
    imap_host = models.CharField(max_length=255, default="imap.gmail.com")
    imap_port = models.PositiveIntegerField(default=993)
    # The SMTP login — always the address itself (a mailbox you own logs in as
    # itself), kept as its own column for the unique constraint.
    username = models.CharField(max_length=320, unique=True)
    password = models.CharField(max_length=255)
    from_address = models.EmailField(max_length=320)
    # Sign-off appended verbatim to every email sent from this box (opener and
    # follow-ups alike). Per box, not global: the signature is part of the sending
    # identity, and a second box is usually a second identity. The email agent is
    # told never to sign (prompts/outreach_agent.j2), so this is the only sign-off.
    # NULL and "" are distinct: NULL means never asked (the onboarding signature
    # step backfills those), "" means the operator declined one and must stick —
    # collapsing them would re-ask a declining operator on every startup.
    signature = models.TextField(blank=True, null=True, default=None)
    # Warm-safe sends/day for this box — *measured*, not configured. Recomputed
    # daily by ``emails/warmth.py`` from the box's own Sent folder, so a mailbox
    # that has been carrying volume for months is trusted with it immediately and
    # one connected an hour ago is not. Enforced at send time, per box (counts
    # this box's outgoing messages since midnight). Persisted rather than derived
    # on demand only because reading it costs an IMAP round trip; it is a cache of
    # the box's own history and safe to discard. The default is the floor, not a
    # working volume: it applies only to a box that has never been measured, and
    # an unmeasured box is one we know nothing about.
    daily_limit = models.PositiveIntegerField(default=WARM_FLOOR_SENDS)
    # The earliest this box may send its next *first* email — the send-spacing clock,
    # rewritten after every first contact as ``now + MIN_SEND_INTERVAL + jitter``.
    # Per box rather than pool-wide because the daily cap is per box too: two boxes
    # are two sending identities, and one receiver's rhythm says nothing about the
    # other's. Replies ignore it entirely (see ``sent_today``). Null = free now.
    next_send_at = models.DateTimeField(null=True, blank=True)

    objects = MailboxManager()

    class Meta:
        verbose_name_plural = "Mailboxes"

    def __str__(self):
        return self.from_address or self.username

    def paused_today(self) -> bool:
        """True when a receiver verdict has stopped this box for the rest of today.

        The receiver's own word beats our measurement: a ``550 5.4.5`` is Gmail
        stating the real ceiling, which no amount of Sent-folder history could
        have discovered. Resets at midnight because that is the horizon the
        verdicts describe.
        """
        from cold_outreach.emails.delivery_policy import PAUSING_RESPONSES
        from cold_outreach.emails.models.maillog import DeliveryEvent

        return DeliveryEvent.objects.filter(
            message__mailbox=self,
            occurred_at__gte=_local_midnight(),
            response__in=PAUSING_RESPONSES,
        ).exists()

    def sent_today(self) -> int:
        """People this box has *first contacted* since local midnight — the cap ledger.

        Counts each of the box's threads by its **first** outgoing message and keeps
        the ones that begin today. So a reply inside a thread opened yesterday is free.
        Scoped to threads that actually belong to a deal — a message sent outside the
        cold-email flow (a test fixture, a future feature) must not spend the cap of a
        conversation that was never opened.

        Cold volume is what a receiver punishes, and answering someone who wrote to
        you is not cold volume — replying within minutes is more human, not less.

        It counts the **transport log**, not the conversation: a send-cap ledger is a
        statement about what left the box, so it belongs on the record of what left
        the box. Derived rather than incremented, so there is nothing to drift after
        a crash.
        """
        from django.db.models import Min

        from cold_outreach.emails.models.maillog import Direction, Message
        from cold_outreach.leads.models import Deal

        opened_today = (
            Message.objects
            .filter(mailbox=self, direction=Direction.OUTBOUND, thread__isnull=False)
            .values("thread_id")
            .annotate(first_sent=Min("sent_at"))
            .filter(first_sent__gte=_local_midnight())
            .values_list("thread_id", flat=True)
        )
        return Deal.objects.filter(thread_id__in=list(opened_today)).count()

    def headroom_today(self) -> int:
        """Sends this box has left today before hitting its measured capacity.

        Zero once the receiver has paused the box, whatever the measurement says.
        """
        if self.paused_today():
            return 0
        return max(0, self.daily_limit - self.sent_today())

    def free_now(self, now=None) -> bool:
        """True when this box's spacing clock has elapsed — cold volume may go.

        The per-box half of `free_for_first_email`, split out because a follow-up
        cannot choose its box: the thread was opened from one, and answering from
        another would break the conversation. So it asks that box whether it is free
        instead of asking the pool for whichever box is freest.

        The window and the daily cap are **not** here. They are pool-level and
        per-day respectively, and both callers check them, so folding them in would
        make this method answer three questions and be reusable for none.
        """
        return self.next_send_at is None or self.next_send_at <= (now or timezone.now())


def has_mailbox() -> bool:
    """True when ≥1 mailbox is configured — i.e. email is a viable channel to
    send from. Gates the find-email leg: with no mailbox there's nothing to send,
    so resolving an address (and spending a credit) is pointless — the leg stays
    idle until a mailbox is connected."""
    return Mailbox.objects.exists()
