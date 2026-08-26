from __future__ import annotations

import smtplib

import pytest

from cold_outreach.emails.delivery_policy import (
    Response,
    classify,
    policy_for,
    record_failure,
)
from cold_outreach.emails.models import DeliveryEvent, Mailbox
from cold_outreach.tests.emails import maillog


def _box() -> Mailbox:
    return maillog.mailbox("a@b.com")


def _verdict(box, response, smtp_code):
    """A recorded refusal against a send from *box* — one row, two facts."""
    return DeliveryEvent.objects.create(
        message=maillog.outbound(box),
        status="rejected" if smtp_code and smtp_code >= 500 else "deferred",
        response=response,
        smtp_code=smtp_code,
    )


class TestClassify:
    def test_deferral_is_a_pacing_signal(self):
        exc = smtplib.SMTPDataError(421, b"4.7.0 Temporary System Problem. Try again later")
        verdict = classify(exc)
        assert verdict.response == Response.DEFERRED
        assert verdict.smtp_code == 421
        assert "Try again later" in verdict.detail

    def test_quota_exceeded_is_distinguished_from_other_550s(self):
        # 5.4.5 and 5.7.1 are both 550; only the enhanced status tells them apart.
        exc = smtplib.SMTPDataError(550, b"5.4.5 Daily user sending quota exceeded")
        assert classify(exc).response == Response.QUOTA_EXCEEDED

    def test_policy_block_is_read_from_the_enhanced_status(self):
        exc = smtplib.SMTPDataError(550, b"5.7.1 Our system has detected ... blocked")
        assert classify(exc).response == Response.BLOCKED

    def test_other_permanent_refusal(self):
        exc = smtplib.SMTPDataError(550, b"5.1.1 The email account does not exist")
        assert classify(exc).response == Response.REFUSED

    def test_auth_failure_is_not_a_reputation_signal(self):
        exc = smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")
        response = classify(exc).response
        # 5.7.8 would otherwise read as a policy block — the exception type wins.
        assert response == Response.AUTH_FAILED
        assert not policy_for(response).from_receiver

    def test_recipients_refused_reads_the_per_recipient_code(self):
        exc = smtplib.SMTPRecipientsRefused({"x@y.com": (550, b"5.4.5 quota exceeded")})
        verdict = classify(exc)
        assert verdict.response == Response.QUOTA_EXCEEDED
        assert verdict.smtp_code == 550

    def test_dropped_connection_carries_no_smtp_code(self):
        verdict = classify(smtplib.SMTPServerDisconnected("connection closed"))
        assert verdict.response == Response.TRANSPORT
        assert verdict.smtp_code is None

    def test_socket_error_is_transport(self):
        assert classify(OSError("network unreachable")).response == Response.TRANSPORT

    def test_reply_without_an_enhanced_status_still_classifies(self):
        assert classify(smtplib.SMTPDataError(451, b"try later")).response == Response.DEFERRED
        assert classify(smtplib.SMTPDataError(554, b"rejected")).response == Response.REFUSED


class TestPolicies:
    def test_a_deferral_does_not_pause_the_box(self):
        # The whole point: a sporadic 421 is routine and costs no capacity.
        policy = policy_for(Response.DEFERRED)
        assert policy.from_receiver
        assert not policy.pause_today

    def test_quota_exceeded_pauses_the_box(self):
        assert policy_for(Response.QUOTA_EXCEEDED).pause_today

    def test_a_block_needs_the_operator(self):
        assert policy_for(Response.BLOCKED).needs_operator

    def test_transport_carries_no_information(self):
        policy = policy_for(Response.TRANSPORT)
        assert not policy.from_receiver
        assert not policy.pause_today


@pytest.mark.django_db
class TestRecordFailure:
    def test_persists_the_verdict_and_returns_its_policy(self):
        send = maillog.outbound(_box())
        policy = record_failure(
            send, smtplib.SMTPDataError(550, b"5.4.5 Daily user sending quota exceeded"),
        )
        assert policy.pause_today
        event = DeliveryEvent.objects.get(message=send)
        assert event.response == Response.QUOTA_EXCEEDED
        assert event.smtp_code == 550
        assert event.enhanced_status == "5.4.5"
        assert event.status == DeliveryEvent.Status.REJECTED

    def test_transport_failure_is_recorded_without_a_code(self):
        send = maillog.outbound(_box())
        record_failure(send, OSError("connection reset"))
        event = DeliveryEvent.objects.get(message=send)
        assert event.smtp_code is None
        # Nothing reached the receiver, so this is not a rejection — a different
        # fact, and the log says which one it is.
        assert event.status == DeliveryEvent.Status.ERROR

    def test_an_accepted_send_is_recorded_with_its_queue_id(self):
        """590 sends left no rows at all, which is why no rate was computable."""
        from cold_outreach.emails.delivery_policy import record_acceptance

        send = maillog.outbound(_box())
        record_acceptance(send, 250, b"2.0.0 OK  1758000000 d9443c01a7336-1f2 - gsmtp")
        event = DeliveryEvent.objects.get(message=send)
        assert event.status == DeliveryEvent.Status.ACCEPTED
        assert event.queue_id == "1758000000"


@pytest.mark.django_db
class TestHeadroomRespectsVerdicts:
    def test_a_paused_box_has_no_headroom(self):
        box = _box()
        box.daily_limit = 40
        box.save()
        assert box.headroom_today() == 40
        _verdict(box, Response.QUOTA_EXCEEDED, 550)
        assert box.headroom_today() == 0

    def test_a_deferral_leaves_headroom_intact(self):
        box = _box()
        box.daily_limit = 40
        box.save()
        _verdict(box, Response.DEFERRED, 421)
        assert box.headroom_today() == 40

    def test_a_paused_box_is_not_offered_for_sending(self):
        box = _box()
        box.daily_limit = 40
        box.save()
        _verdict(box, Response.BLOCKED, 550)
        assert Mailbox.objects.remaining_today() == 0
        assert Mailbox.objects.free_for_first_email() is None

    def test_sending_falls_through_to_an_unpaused_box(self):
        paused = _box()
        paused.daily_limit = 40
        paused.save()
        _verdict(paused, Response.QUOTA_EXCEEDED, 550)
        healthy = Mailbox.objects.create(
            username="c@d.com", password="pw", from_address="c@d.com",
            daily_limit=10,
        )
        assert Mailbox.objects.free_for_first_email() == healthy
        assert Mailbox.objects.remaining_today() == 10
