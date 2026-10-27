# openoutsend/tests/emails/test_resend_inbound.py
"""A resend box reads its replies: Resend Inbound polled into the mail log.

The API is mocked at its edge — ``resend.received_emails`` and ``resend.received_raw``
for the poll, ``resend._request`` and ``urlopen`` for the client's own tests.
"""
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from openoutsend.emails import resend, resend_events, resend_inbound
from openoutsend.emails.mail_pass import run_mail_pass
from openoutsend.emails.models import FolderCoverage, Kind, Message, Transport
from openoutsend.emails.sender import send_email
from openoutsend.errors import OutsendError
from openoutsend.leads.models import DealState
from openoutsend.planner import unanswered_replies
from openoutsend.tests.emails import maillog
from openoutsend.tests.emails.fake_imap import RECEIVED_AT
from openoutsend.tests.factories import DealFactory, LeadFactory

REPLY_TO = "eracle@reply.openoutreach.app"
ROOT = "root@openoutreach.app"
UNSUBSCRIBE = "https://hub.openoutreach.app/unsubscribe/tok/"


@pytest.fixture
def reply_to(monkeypatch):
    monkeypatch.setenv("OUTSEND_REPLY_TO", REPLY_TO)


def _resend_box():
    return maillog.mailbox("eracle@openoutreach.app", watching=False,
                           transport=Transport.RESEND, host="api.resend.com", port=443,
                           imap_host="", imap_port=0)


def _raw(message_id, *, sender="lead@corp.com", to=REPLY_TO, references=f"<{ROOT}>",
         body="Most of these are already our clients."):
    lines = [
        f"From: {sender}",
        f"To: {to}",
        "Subject: Re: Ten leads",
        f"Message-ID: <{message_id}>",
        "Date: Mon, 16 Mar 2026 10:00:00 +0000",
        "Content-Type: text/plain; charset=utf-8",
    ]
    if references:
        lines.append(f"References: {references}")
    return ("\r\n".join(lines) + "\r\n\r\n" + body).encode()


def _listed(email_id, message_id, *, to=REPLY_TO, minutes_ago=1):
    created = timezone.now() - timedelta(minutes=minutes_ago)
    return {"id": email_id, "to": [to], "cc": [], "message_id": f"<{message_id}>",
            "created_at": created.isoformat()}


def _poll(box, listed, raws):
    """Poll with *listed* as the received list and *raws* as id → bytes (or an exception)."""
    def fetch(_key, email_id):
        found = raws[email_id]
        if isinstance(found, Exception):
            raise found
        return found

    with patch.object(resend, "received_emails", return_value=list(listed)) as listing, \
            patch.object(resend, "received_raw", side_effect=fetch) as fetched:
        stored = resend_inbound.poll(box)
    return stored, listing, fetched


@pytest.mark.django_db
@pytest.mark.usefixtures("reply_to")
class TestPoll:
    def test_a_received_email_is_stored_as_the_bytes_that_arrived(self):
        box = _resend_box()
        raw = _raw("r1@corp.com")
        stored, _, _ = _poll(box, [_listed("em_1", "r1@corp.com")], {"em_1": raw})

        assert stored == 1
        row = Message.objects.get(message_id="r1@corp.com")
        assert bytes(row.raw) == raw
        assert (row.folder, row.from_address, row.references_ids) == (
            "resend", "lead@corp.com", [ROOT])

    def test_a_repoll_downloads_nothing_and_stores_nothing(self):
        box = _resend_box()
        listed = [_listed("em_1", "r1@corp.com")]
        _poll(box, listed, {"em_1": _raw("r1@corp.com")})

        stored, _, fetched = _poll(box, listed, {"em_1": _raw("r1@corp.com")})
        assert stored == 0
        fetched.assert_not_called()
        assert Message.objects.count() == 1

    def test_a_failed_fetch_is_not_stepped_over(self):
        box = _resend_box()
        # Newest first, as the API lists them: em_2 arrived after em_1.
        listed = [_listed("em_2", "r2@corp.com"), _listed("em_1", "r1@corp.com", minutes_ago=2)]

        stored, _, _ = _poll(box, listed, {"em_1": OSError("expired"),
                                           "em_2": _raw("r2@corp.com")})
        assert stored == 0
        coverage = FolderCoverage.objects.get(mailbox=box, folder="resend")
        assert coverage.synced_at is None

        stored, listing, _ = _poll(box, listed, {"em_1": _raw("r1@corp.com"),
                                                 "em_2": _raw("r2@corp.com")})
        assert stored == 2
        assert listing.call_args.kwargs["since"] is None

    def test_an_email_with_no_original_file_stops_the_walk(self):
        box = _resend_box()
        stored, _, _ = _poll(box, [_listed("em_1", "r1@corp.com")], {"em_1": None})
        assert stored == 0
        assert FolderCoverage.objects.get(mailbox=box, folder="resend").synced_at is None

    def test_a_complete_walk_moves_the_cursor(self):
        box = _resend_box()
        before = timezone.now()
        _poll(box, [_listed("em_1", "r1@corp.com")], {"em_1": _raw("r1@corp.com")})
        synced = FolderCoverage.objects.get(mailbox=box, folder="resend").synced_at
        assert synced >= before

        _, listing, _ = _poll(box, [], {})
        assert listing.call_args.kwargs["since"] == synced - resend_inbound.CURSOR_MARGIN

    def test_mail_received_on_another_domain_is_not_this_boxs(self):
        box = _resend_box()
        listed = [_listed("em_1", "r1@corp.com", to="someone@elsewhere.com")]
        stored, _, fetched = _poll(box, listed, {})
        assert stored == 0
        fetched.assert_not_called()

    def test_mail_tied_to_no_conversation_is_still_kept(self):
        box = _resend_box()
        raw = _raw("r1@corp.com", sender="stranger@else.com", references=None)
        stored, _, _ = _poll(box, [_listed("em_1", "r1@corp.com")], {"em_1": raw})
        assert stored == 1

    def test_an_unreachable_api_stores_nothing_and_does_not_raise(self):
        box = _resend_box()
        with patch.object(resend, "received_emails", side_effect=OSError("down")):
            assert resend_inbound.poll(box) == 0


@pytest.mark.django_db
def test_no_reply_address_means_no_call(monkeypatch):
    monkeypatch.delenv("OUTSEND_REPLY_TO", raising=False)
    with patch.object(resend, "received_emails") as listing:
        assert resend_inbound.poll(_resend_box()) == 0
    listing.assert_not_called()


@pytest.mark.django_db
@pytest.mark.usefixtures("reply_to")
def test_a_polled_reply_reaches_its_deal_and_is_owed_an_answer():
    box = _resend_box()
    sent = maillog.outbound(box, to="lead@corp.com", message_id=ROOT,
                            sent_at=RECEIVED_AT - timedelta(days=1))
    deal = DealFactory(lead=LeadFactory(email="lead@corp.com"), state=DealState.EMAILED,
                       mailbox=box, email_subject="Ten leads", thread=sent.thread)

    with patch.object(resend, "received_emails",
                      return_value=[_listed("em_1", "r1@corp.com")]), \
            patch.object(resend, "received_raw", return_value=_raw("r1@corp.com")), \
            patch.object(resend_events, "poll", return_value=0):
        mirrored, _, _ = run_mail_pass()

    assert mirrored == 1
    reply = Message.objects.get(message_id="r1@corp.com")
    assert (reply.kind, reply.thread_id) == (Kind.HUMAN_REPLY, deal.thread_id)
    assert list(unanswered_replies()) == [deal]


# ── The reply address on a send ───────────────────────────────────


@pytest.mark.django_db
class TestReplyTo:
    def _send(self, box):
        with patch.object(resend, "send", return_value="em_1") as post:
            row = send_email(box, "lead@corp.com", "Ten leads", "Body",
                             unsubscribe_url=UNSUBSCRIBE)
        return row, post.call_args.args[1]

    @pytest.mark.usefixtures("reply_to")
    def test_a_resend_send_names_the_reply_address(self):
        row, payload = self._send(_resend_box())
        assert payload["reply_to"] == [REPLY_TO]
        assert f"Reply-To: {REPLY_TO}" in bytes(row.raw).decode()

    def test_without_one_replies_go_to_the_from_address(self, monkeypatch):
        monkeypatch.delenv("OUTSEND_REPLY_TO", raising=False)
        _, payload = self._send(_resend_box())
        assert "reply_to" not in payload

    def test_a_malformed_one_stops_the_send(self, monkeypatch):
        monkeypatch.setenv("OUTSEND_REPLY_TO", "reply.openoutreach.app")
        with patch.object(resend, "send") as post, pytest.raises(OutsendError):
            send_email(_resend_box(), "lead@corp.com", "Ten leads", "Body",
                       unsubscribe_url=UNSUBSCRIBE)
        post.assert_not_called()


# ── The client ────────────────────────────────────────────────────


class TestReceivedRaw:
    def test_downloads_the_original_file_without_the_key(self):
        answer = {"raw": {"download_url": "https://cdn.resend.dev/raw/em_1"}}
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"raw bytes"
        with patch.object(resend, "_request", return_value=answer), \
                patch.object(resend.urllib.request, "urlopen", return_value=response) as get:
            assert resend.received_raw("re_key", "em_1") == b"raw bytes"
        request = get.call_args.args[0]
        assert request.full_url == "https://cdn.resend.dev/raw/em_1"
        assert request.get_header("Authorization") is None

    def test_no_original_file_is_none(self):
        with patch.object(resend, "_request", return_value={"raw": None}):
            assert resend.received_raw("re_key", "em_1") is None


def test_the_received_list_with_no_since_reads_every_page():
    def email(email_id, days_ago):
        created = timezone.now() - timedelta(days=days_ago)
        return {"id": email_id, "created_at": created.isoformat()}

    pages = [{"has_more": True, "data": [email("a", 1)]},
             {"has_more": False, "data": [email("b", 400)]}]
    with patch.object(resend, "_request", side_effect=pages) as request, \
            patch.object(resend.time, "sleep"):
        emails = resend.received_emails("re_key", since=None)
    assert [item["id"] for item in emails] == ["a", "b"]
    assert request.call_args_list[0].args[1].startswith("/emails/receiving?")
