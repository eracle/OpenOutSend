# tests/emails/test_sync.py
"""**sync** — the transport half. Bytes land; nothing decides what they mean.

The incident this file exists for: a box lost UIDs 27 and 28 permanently, because
the walk classified as it read and advanced a cursor over everything it had *seen*.
The rules under test are the two that make that impossible — identity is the
Message-ID, and a message that cannot be fetched is not stepped over.
"""
from unittest.mock import patch

import pytest

from cold_outreach.emails.models import Direction, FolderCoverage, Message
from cold_outreach.emails.sync import mirror
from tests.emails import maillog
from tests.emails.fake_imap import FakeIMAP, bounce, message

SENDER = "s@infra.com"


def _box():
    return maillog.mailbox(SENDER, daily_limit=10)


def _mirror(box, fake) -> int:
    with patch("cold_outreach.emails.sync._connect", return_value=fake):
        return mirror(box)


def _coverage(box) -> FolderCoverage:
    return FolderCoverage.objects.get(mailbox=box, folder="INBOX")


@pytest.mark.django_db
class TestFirstSight:
    """A connected mailbox is a real one; its history is not our record."""

    def test_the_first_pass_starts_at_the_high_water_mark(self):
        box = maillog.mailbox(SENDER, watching=False)
        history = [message(uid, to=SENDER, sender="old@friend.com") for uid in range(1, 40)]

        assert _mirror(box, FakeIMAP(history)) == 0
        assert _coverage(box).last_uid == 39

    def test_mail_arriving_after_the_first_pass_is_mirrored(self):
        box = maillog.mailbox(SENDER, watching=False)
        history = [message(uid, to=SENDER, sender="old@friend.com") for uid in range(1, 40)]
        _mirror(box, FakeIMAP(history))
        maillog.outbound(box, to="p@corp.com")
        arrived = history + [message(40, to=SENDER, sender="p@corp.com")]

        assert _mirror(box, FakeIMAP(arrived)) == 1

    def test_an_operator_can_still_ask_for_the_history(self):
        """Identity is the Message-ID, so winding coverage back is safe and free."""
        box = maillog.mailbox(SENDER, watching=False)
        maillog.outbound(box, to="old@friend.com")
        history = [message(uid, to=SENDER, sender="old@friend.com") for uid in range(1, 5)]
        _mirror(box, FakeIMAP(history))

        coverage = _coverage(box)
        coverage.last_uid = 0
        coverage.save(update_fields=["last_uid"])

        assert _mirror(box, FakeIMAP(history)) == 4


@pytest.mark.django_db
class TestWhoseMailIsStored:
    """The operator's mailbox is their real one; only our conversation is mirrored."""

    def test_a_stranger_is_not_stored_at_all(self):
        box = _box()

        assert _mirror(box, FakeIMAP([message(7, to=SENDER, sender="stranger@x.com")])) == 0
        assert Message.objects.count() == 0

    def test_a_strangers_body_is_never_even_fetched(self):
        box = _box()
        fake = FakeIMAP([message(7, to=SENDER, sender="stranger@x.com")])

        _mirror(box, fake)

        assert fake.body_fetches == []

    def test_the_walk_still_advances_past_what_it_declined(self):
        """Skipping is a decision we make every pass, not a message left pending."""
        box = _box()
        _mirror(box, FakeIMAP([message(7, to=SENDER, sender="stranger@x.com")]))

        coverage = _coverage(box)
        assert coverage.last_uid == 7
        assert coverage.synced_at is not None

    def test_a_message_naming_an_id_we_sent_is_stored(self):
        box = _box()
        maillog.outbound(box, to="p@corp.com", message_id="root@infra.com")

        assert _mirror(box, FakeIMAP([message(7, to=SENDER, sender="p@corp.com",
                                              references="<root@infra.com>")])) == 1

        row = Message.objects.get(direction=Direction.INBOUND)
        assert row.kind == ""            # sync has no opinion
        assert row.processed_at is None  # pending, not "nothing to read"

    def test_mail_from_someone_this_box_wrote_to_is_stored(self):
        """The repair path: a reply we mis-thread is still there to be re-read."""
        box = _box()
        maillog.outbound(box, to="p@corp.com")

        # No threading headers at all.
        assert _mirror(box, FakeIMAP([message(7, to=SENDER, sender="p@corp.com")])) == 1

    def test_the_unsub_alias_is_stored_whoever_it_is_from(self):
        box = _box()

        assert _mirror(box, FakeIMAP([message(7, to="s+unsub@infra.com",
                                              sender="never@mailed.com")])) == 1

    def test_a_bounce_is_stored_though_no_daemon_was_ever_mailed(self):
        """A report comes from the receiver's daemon and is still about our send."""
        box = _box()
        maillog.outbound(box, to="p@corp.com", message_id="root@infra.com")

        assert _mirror(box, FakeIMAP([bounce(7, to=SENDER,
                                             original="<root@infra.com>")])) == 1

    def test_provenance_is_kept_without_becoming_identity(self):
        box = _box()
        maillog.outbound(box, to="x@y.com")
        _mirror(box, FakeIMAP([message(7, to=SENDER, sender="x@y.com")], uidvalidity=42))

        row = Message.objects.get(direction=Direction.INBOUND)
        assert (row.folder, row.uid, row.uidvalidity) == ("INBOX", 7, 42)
        assert row.message_id == "m7@corp.com"   # the id, not the UID

    def test_a_message_with_no_id_is_keyed_on_its_own_bytes(self):
        box = _box()
        maillog.outbound(box, to="x@y.com")
        uid, raw = message(7, to=SENDER, sender="x@y.com")
        raw = b"\r\n".join(line for line in raw.split(b"\r\n")
                           if not line.startswith(b"Message-ID"))

        _mirror(box, FakeIMAP([(uid, raw)]))

        assert Message.objects.get(direction=Direction.INBOUND).message_id.startswith(
            "sha256:")


@pytest.mark.django_db
class TestTheCursorIsOnlyAnOptimisation:
    def test_rewalking_the_whole_box_stores_nothing_new(self):
        """Reset coverage to zero and the pass is free — identity is the Message-ID."""
        box = _box()
        maillog.outbound(box, to="x@y.com")
        fake = FakeIMAP([message(7, to=SENDER, sender="x@y.com")])

        assert _mirror(box, fake) == 1
        coverage = _coverage(box)
        coverage.last_uid = 0
        coverage.save(update_fields=["last_uid"])

        assert _mirror(box, fake) == 0
        assert Message.objects.filter(direction=Direction.INBOUND).count() == 1

    def test_the_cursor_advances_only_over_what_was_stored(self):
        box = _box()
        maillog.outbound(box, to="x@y.com")
        maillog.outbound(box, to="y@z.com")
        _mirror(box, FakeIMAP([message(7, to=SENDER, sender="x@y.com"),
                               message(9, to=SENDER, sender="y@z.com")]))
        assert _coverage(box).last_uid == 9

    def test_a_changed_uidvalidity_restarts_the_walk(self):
        box = _box()
        maillog.outbound(box, to="x@y.com")
        maillog.outbound(box, to="z@z.com")
        _mirror(box, FakeIMAP([message(7, to=SENDER, sender="x@y.com")]))

        # Same UID, different epoch: a different message entirely.
        reissued = FakeIMAP([message(7, to=SENDER, sender="z@z.com",
                                     message_id="<other@corp.com>")], uidvalidity=2)
        assert _mirror(box, reissued) == 1
        assert reissued.searched == ["1:*"]
        assert Message.objects.filter(direction=Direction.INBOUND).count() == 2

    def test_synced_at_only_moves_on_a_walk_that_finished(self):
        box = _box()
        maillog.outbound(box, to="x@y.com")
        maillog.outbound(box, to="y@z.com")
        fake = FakeIMAP([message(7, to=SENDER, sender="x@y.com"),
                         message(8, to=SENDER, sender="y@z.com")], unreadable={8})

        _mirror(box, fake)

        coverage = _coverage(box)
        assert coverage.last_uid == 7        # stopped in front of 8
        assert coverage.synced_at is None    # and says so


@pytest.mark.django_db
class TestAnUnfetchableMessageIsNotSteppedOver:
    def test_the_walk_stops_and_the_next_pass_retries(self):
        box = _box()
        maillog.outbound(box, to="x@y.com")
        messages = [message(uid, to=SENDER, sender="x@y.com") for uid in (7, 8, 9)]

        broken = FakeIMAP(messages, unreadable={8})
        assert _mirror(box, broken) == 1
        assert _coverage(box).last_uid == 7

        # Whatever was wrong with UID 8 clears; both it and 9 are still read.
        assert _mirror(box, FakeIMAP(messages)) == 2
        assert sorted(Message.objects.filter(direction=Direction.INBOUND)
                      .values_list("uid", flat=True)) == [7, 8, 9]

    def test_an_unreachable_box_leaves_coverage_untouched(self):
        box = _box()
        maillog.outbound(box, to="x@y.com")
        _mirror(box, FakeIMAP([message(7, to=SENDER, sender="x@y.com")]))

        with patch("cold_outreach.emails.sync._connect", side_effect=OSError("no route")):
            assert mirror(box) == 0

        assert _coverage(box).last_uid == 7


@pytest.mark.django_db
class TestThreadingOnInsert:
    def test_a_reply_joins_the_thread_it_names(self):
        box = _box()
        sent = maillog.outbound(box, to="p@corp.com", message_id="root@infra.com")

        _mirror(box, FakeIMAP([message(7, to=SENDER, sender="p@corp.com",
                                       references="<root@infra.com>")]))

        assert Message.objects.get(direction="in").thread_id == sent.thread_id

    def test_in_reply_to_alone_is_enough(self):
        """The case root-matching dropped: the header points at the newest message."""
        box = _box()
        sent = maillog.outbound(box, to="p@corp.com", message_id="root@infra.com")
        second = maillog.outbound(box, thread=sent.thread, to="p@corp.com",
                                  message_id="second@infra.com")

        _mirror(box, FakeIMAP([message(7, to=SENDER, sender="p@corp.com",
                                       in_reply_to=f"<{second.message_id}>")]))

        assert Message.objects.get(direction="in").thread_id == sent.thread_id

    def test_out_of_order_arrival_merges_two_threads(self):
        """The reply lands before the message it answers; both end up in one thread."""
        box = _box()
        maillog.outbound(box, to="p@corp.com")
        _mirror(box, FakeIMAP([message(7, to=SENDER, sender="p@corp.com",
                                       message_id="<child@corp.com>",
                                       in_reply_to="<parent@corp.com>")]))
        _mirror(box, FakeIMAP([message(7, to=SENDER, sender="p@corp.com",
                                       message_id="<child@corp.com>",
                                       in_reply_to="<parent@corp.com>"),
                               message(8, to=SENDER, sender="p@corp.com",
                                       message_id="<parent@corp.com>")]))

        threads = set(Message.objects.filter(direction=Direction.INBOUND)
                      .values_list("thread_id", flat=True))
        assert len(threads) == 1

    def test_another_boxs_thread_is_never_joined(self):
        """An id we sent from a different box must not attach a reply to it."""
        box = _box()
        other = maillog.mailbox("o@infra.com")
        maillog.outbound(other, to="p@corp.com", message_id="root@infra.com")
        ours = maillog.outbound(box, to="p@corp.com", message_id="ours@infra.com")

        _mirror(box, FakeIMAP([message(7, to=SENDER, sender="p@corp.com",
                                       references="<root@infra.com>")]))

        row = Message.objects.get(mailbox=box, direction=Direction.INBOUND)
        assert row.thread.mailbox_id == box.pk
        assert row.thread_id != ours.thread_id
