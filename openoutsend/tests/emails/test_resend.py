# openoutsend/tests/emails/test_resend.py
"""The resend transport: the API client, the send, the bounce poll, and what it skips.

The API is mocked at its edge — ``resend._request`` for the client's own tests, and
``resend.send`` / ``resend.recent_emails`` for everything above it.
"""
import io
import json
import urllib.error
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from openoutsend.emails import resend, resend_events
from openoutsend.emails.delivery_policy import Response, classify
from openoutsend.emails.models import DeliveryEvent, Message, Thread, Transport
from openoutsend.emails.sender import OPT_OUT_LINE, Refused, send_email
from openoutsend.errors import OutsendError
from openoutsend.leads.models import DealState, Outcome
from openoutsend.leads.suppression import is_suppressed
from openoutsend.tests.emails import maillog
from openoutsend.tests.factories import DealFactory, LeadFactory

UNSUBSCRIBE = "https://hub.openoutreach.app/unsubscribe/tok/"


def _resend_box(address="eracle@openoutreach.app"):
    return maillog.mailbox(address, watching=False, transport=Transport.RESEND,
                           host="api.resend.com", port=443, imap_host="", imap_port=0)


# ── The client ────────────────────────────────────────────────────


class TestVerify:
    def test_a_verified_domain_passes(self):
        domains = {"data": [{"name": "openoutreach.app", "status": "verified"}]}
        with patch.object(resend, "_request", return_value=domains):
            assert resend.verify("re_key", "eracle@openoutreach.app") == (True, "")

    def test_a_sending_only_key_is_refused_by_name(self):
        refusal = resend.ResendError(401, "restricted_api_key", "This key can only send")
        with patch.object(resend, "_request", side_effect=refusal):
            ok, reason = resend.verify("re_key", "eracle@openoutreach.app")
        assert not ok
        assert "full-access key" in reason

    def test_an_unverified_domain_is_refused(self):
        domains = {"data": [{"name": "openoutreach.app", "status": "pending"}]}
        with patch.object(resend, "_request", return_value=domains):
            ok, reason = resend.verify("re_key", "eracle@openoutreach.app")
        assert not ok
        assert "pending" in reason

    def test_a_domain_not_on_the_account_is_refused(self):
        with patch.object(resend, "_request", return_value={"data": []}):
            ok, reason = resend.verify("re_key", "eracle@openoutreach.app")
        assert not ok
        assert "not a domain on this Resend account" in reason

    def test_an_unreachable_api_is_a_reason_not_a_crash(self):
        with patch.object(resend, "_request", side_effect=OSError("no route")):
            ok, reason = resend.verify("re_key", "eracle@openoutreach.app")
        assert not ok
        assert "could not reach Resend" in reason


class TestRecentEmails:
    def _email(self, email_id, minutes_ago):
        created = timezone.now() - timedelta(minutes=minutes_ago)
        return {"id": email_id, "created_at": created.isoformat(), "last_event": "delivered"}

    def test_pages_back_until_it_passes_since(self):
        pages = [
            {"has_more": True, "data": [self._email("a", 1), self._email("b", 2)]},
            {"has_more": True, "data": [self._email("c", 3), self._email("d", 120)]},
        ]
        with patch.object(resend, "_request", side_effect=pages) as request, \
                patch.object(resend.time, "sleep"):
            emails = resend.recent_emails("re_key", since=timezone.now() - timedelta(minutes=60))
        assert [email["id"] for email in emails] == ["a", "b", "c"]
        assert "after=b" in request.call_args_list[1].args[1]

    def test_stops_when_the_list_runs_out(self):
        page = {"has_more": False, "data": [self._email("a", 1)]}
        with patch.object(resend, "_request", return_value=page) as request:
            emails = resend.recent_emails("re_key", since=timezone.now() - timedelta(days=1))
        assert [email["id"] for email in emails] == ["a"]
        assert request.call_count == 1


def test_an_api_refusal_carries_its_status_and_name():
    body = io.BytesIO(json.dumps({"name": "daily_quota_exceeded", "message": "over"}).encode())
    error = urllib.error.HTTPError("https://api.resend.com/emails", 429, "Too Many", {}, body)
    with patch.object(resend.urllib.request, "urlopen", side_effect=error):
        with pytest.raises(resend.ResendError) as raised:
            resend.send("re_key", {}, idempotency_key="k")
    assert (raised.value.status, raised.value.name) == (429, "daily_quota_exceeded")


@pytest.mark.parametrize("status, name, expected", [
    (429, "daily_quota_exceeded", Response.QUOTA_EXCEEDED),
    (429, "rate_limit_exceeded", Response.DEFERRED),
    (403, "validation_error", Response.AUTH_FAILED),
    (422, "invalid_parameter", Response.REFUSED),
    (500, "application_error", Response.TRANSPORT),
])
def test_resend_refusals_are_read_like_smtp_ones(status, name, expected):
    assert classify(resend.ResendError(status, name, "")).response == expected


TO_REFUSED = ("Invalid `to` field. Please use our testing email address instead of domains "
              "like `example.com`.")


def test_a_refused_to_field_reads_as_an_address_that_never_could_work():
    verdict = classify(resend.ResendError(422, "validation_error", TO_REFUSED))
    assert (verdict.response, verdict.enhanced_status) == (Response.REFUSED, "5.1.3")


def test_a_validation_error_on_another_field_says_nothing_about_the_address():
    refusal = resend.ResendError(422, "validation_error", "Invalid `from` field.")
    assert classify(refusal).enhanced_status == ""


# ── The send ──────────────────────────────────────────────────────


@pytest.mark.django_db
class TestResendSend:
    def _send(self, box, **kwargs):
        with patch.object(resend, "send", return_value="em_1") as post:
            row = send_email(box, "lead@corp.com", "Ten leads", "Body",
                             unsubscribe_url=UNSUBSCRIBE, **kwargs)
        return row, post.call_args

    def test_the_opener_carries_the_one_click_link_and_names_itself(self):
        row, call = self._send(_resend_box())
        payload, key = call.args[1], call.kwargs["idempotency_key"]
        headers = payload["headers"]
        assert headers["List-Unsubscribe"] == f"<{UNSUBSCRIBE}>"
        assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
        assert headers["References"] == key == f"<{row.message_id}>"
        assert "In-Reply-To" not in headers

    def test_the_footer_is_the_link_not_a_reply_word(self):
        _, call = self._send(_resend_box())
        text = call.args[1]["text"]
        assert UNSUBSCRIBE in text
        assert OPT_OUT_LINE not in text

    def test_a_later_step_threads_on_the_chain(self):
        box = _resend_box()
        _, call = self._send(box, in_reply_to="<first@openoutreach.app>",
                             references="<first@openoutreach.app>")
        headers = call.args[1]["headers"]
        assert headers["In-Reply-To"] == headers["References"] == "<first@openoutreach.app>"

    def test_the_resend_id_is_the_acceptance_handle(self):
        row, _ = self._send(_resend_box())
        event = row.delivery_events.get()
        assert (event.status, event.queue_id) == (DeliveryEvent.Status.ACCEPTED, "em_1")

    def test_no_link_means_no_send_and_no_row(self):
        box = _resend_box()
        with patch.object(resend, "send") as post, pytest.raises(OutsendError):
            send_email(box, "lead@corp.com", "Ten leads", "Body")
        post.assert_not_called()
        assert not Message.objects.exists()

    def test_a_quota_refusal_is_recorded_and_pauses_the_box(self):
        box = _resend_box()
        refusal = resend.ResendError(429, "daily_quota_exceeded", "over")
        with patch.object(resend, "send", side_effect=refusal), pytest.raises(Refused) as raised:
            send_email(box, "lead@corp.com", "Ten leads", "Body", unsubscribe_url=UNSUBSCRIBE)
        assert raised.value.cause is refusal
        assert box.paused_today()

    def test_a_refused_to_field_suppresses_the_address(self):
        box = _resend_box()
        refusal = resend.ResendError(422, "validation_error", TO_REFUSED)
        with patch.object(resend, "send", side_effect=refusal), pytest.raises(Refused):
            send_email(box, "you@example.com", "Ten leads", "Body", unsubscribe_url=UNSUBSCRIBE)
        assert is_suppressed("you@example.com")
        assert not box.paused_today()

    def test_a_mailbox_keeps_its_mailto_opt_out(self):
        box = maillog.mailbox("s@corp.com")
        with patch("openoutsend.emails.sender._deliver") as deliver:
            send_email(box, "lead@corp.com", "Hi", "Body", unsubscribe_url=UNSUBSCRIBE)
        message = deliver.call_args.args[1]
        assert message["List-Unsubscribe"].startswith("<mailto:")
        assert message["References"] is None


@pytest.mark.django_db
def test_the_step_sends_the_link_its_line_was_piped_with(operator):
    from openoutsend.core.agents.outreach import OutreachDecision
    from openoutsend.core.sequences import cold
    from openoutsend.emails.steps.send import send_first_email

    box = _resend_box()
    deal = DealFactory(lead=LeadFactory(email="lead@corp.com"), state=DealState.READY,
                       fields={"unsubscribe_url": UNSUBSCRIBE})
    decision = OutreachDecision(action="send_message", subject="Hi", message="Body")
    with patch("openoutsend.core.agents.outreach.run_outreach_agent", return_value=decision), \
            patch("openoutsend.leads.summaries.materialize_profile_summary_if_missing"), \
            patch.object(resend, "send", return_value="em_1") as post:
        send_first_email(deal, box, cold().first, None)
    assert post.call_args.args[1]["headers"]["List-Unsubscribe"] == f"<{UNSUBSCRIBE}>"


# ── The capacity ──────────────────────────────────────────────────


@pytest.mark.django_db
class TestCapacity:
    def test_a_resend_box_has_the_declared_cap(self, monkeypatch):
        monkeypatch.delenv("OUTSEND_DAILY_CAP", raising=False)
        assert _resend_box().capacity() == 50

    def test_the_cap_is_read_fresh(self, monkeypatch):
        monkeypatch.setenv("OUTSEND_DAILY_CAP", "12")
        assert _resend_box().headroom_today() == 12

    def test_a_mailbox_keeps_its_measured_one(self, monkeypatch):
        monkeypatch.setenv("OUTSEND_DAILY_CAP", "12")
        assert maillog.mailbox("s@corp.com", daily_limit=7).capacity() == 7


def _first_contact(box, sequence):
    """One person first contacted today from *box*, in *sequence* — what the ledger counts."""
    deal = DealFactory(sequence=sequence, state=DealState.EMAILED, mailbox=box,
                       thread=Thread.objects.create(mailbox=box))
    maillog.outbound(box, thread=deal.thread, to=deal.lead.email, sent_at=timezone.now())


@pytest.mark.django_db
class TestCapPerSequence:
    """Two sequences on one resend box each spend their own declared budget."""

    def test_a_sequence_counts_its_own_first_contacts(self, monkeypatch):
        monkeypatch.setenv("OUTSEND_DAILY_CAP", "3")
        box = _resend_box()
        _first_contact(box, "relaunch")
        _first_contact(box, "relaunch")
        _first_contact(box, "sample-to-order")

        assert box.headroom_today("relaunch") == 1
        assert box.headroom_today("sample-to-order") == 2

    def test_unscoped_it_counts_the_whole_box(self, monkeypatch):
        monkeypatch.setenv("OUTSEND_DAILY_CAP", "3")
        box = _resend_box()
        _first_contact(box, "relaunch")
        _first_contact(box, "sample-to-order")

        assert box.headroom_today() == 1

    def test_a_mailbox_spends_one_budget_whatever_the_sequence(self):
        """A mailbox's cap is the reputation it measured, which every sequence spends."""
        box = maillog.mailbox("s@corp.com", daily_limit=3)
        _first_contact(box, "relaunch")
        _first_contact(box, "relaunch")

        assert box.headroom_today("sample-to-order") == 1

    def test_one_sequence_out_of_budget_leaves_the_box_to_another(self, monkeypatch):
        from openoutsend.planner import free_for_first_email

        monkeypatch.setenv("OUTSEND_DAILY_CAP", "1")
        box = _resend_box()
        _first_contact(box, "relaunch")

        with patch("openoutsend.core.sending_window.within_sending_window", return_value=True):
            assert free_for_first_email("relaunch") is None
            assert free_for_first_email("sample-to-order") == box


@pytest.mark.django_db
def test_the_warm_ramp_never_measures_a_resend_box():
    from openoutsend.emails.warmth import measure_pool

    _resend_box()
    with patch("openoutsend.emails.warmth.refresh_capacity") as refresh:
        measure_pool()
    refresh.assert_not_called()


@pytest.mark.django_db
def test_the_mail_pass_polls_a_resend_box_instead_of_reading_it():
    from openoutsend.emails.mail_pass import run_mail_pass

    box = _resend_box()
    with patch("openoutsend.emails.sync.mirror") as mirror, \
            patch.object(resend_events, "poll", return_value=2) as poll:
        _, _, projected = run_mail_pass()
    mirror.assert_not_called()
    poll.assert_called_once_with(box)
    assert projected == 2


# ── The bounce poll ───────────────────────────────────────────────


@pytest.mark.django_db
class TestPoll:
    def _accepted(self, box, email_id="em_1", to="lead@corp.com"):
        deal = DealFactory(lead=LeadFactory(email=to), state=DealState.EMAILED)
        row = maillog.outbound(box, to=to)
        DeliveryEvent.objects.create(message=row, status=DeliveryEvent.Status.ACCEPTED,
                                     queue_id=email_id)
        return deal, row

    def _poll(self, box, *emails):
        with patch.object(resend, "recent_emails", return_value=list(emails)) as listed:
            recorded = resend_events.poll(box)
        return recorded, listed

    def test_a_bounce_is_recorded_and_ends_the_address(self):
        box = _resend_box()
        deal, row = self._accepted(box)
        recorded, _ = self._poll(box, {"id": "em_1", "last_event": "bounced"})
        assert recorded == 1
        assert row.delivery_events.filter(status=DeliveryEvent.Status.BOUNCED).exists()
        assert is_suppressed("lead@corp.com")
        deal.refresh_from_db()
        assert (deal.state, deal.outcome) == (DealState.COMPLETED, Outcome.UNDELIVERABLE)

    def test_a_complaint_is_an_unsubscribe(self):
        box = _resend_box()
        deal, row = self._accepted(box)
        self._poll(box, {"id": "em_1", "last_event": "complained"})
        assert row.delivery_events.filter(status=DeliveryEvent.Status.COMPLAINED).exists()
        deal.refresh_from_db()
        assert deal.outcome == Outcome.UNSUBSCRIBED

    def test_a_delivery_records_nothing(self):
        box = _resend_box()
        self._accepted(box)
        recorded, _ = self._poll(box, {"id": "em_1", "last_event": "delivered"})
        assert recorded == 0
        assert not is_suppressed("lead@corp.com")

    def test_an_email_that_is_not_ours_is_ignored(self):
        box = _resend_box()
        self._accepted(box)
        recorded, _ = self._poll(box, {"id": "someone-elses", "last_event": "bounced"})
        assert recorded == 0

    def test_a_settled_send_is_not_asked_about_again(self):
        box = _resend_box()
        self._accepted(box)
        self._poll(box, {"id": "em_1", "last_event": "bounced"})
        recorded, listed = self._poll(box, {"id": "em_1", "last_event": "bounced"})
        assert recorded == 0
        listed.assert_not_called()

    def test_nothing_sent_means_no_call(self):
        recorded, listed = self._poll(_resend_box())
        assert recorded == 0
        listed.assert_not_called()

    def test_an_unreachable_api_records_nothing_and_does_not_raise(self):
        box = _resend_box()
        self._accepted(box)
        with patch.object(resend, "recent_emails", side_effect=OSError("down")):
            assert resend_events.poll(box) == 0
