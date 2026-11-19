"""One bounded pass: read, end, wait, converse, send — in that order, and then stop.

The steps themselves are tested next to their own code; what is tested here is the
pass that drives them — the order, the guards it obeys as bounds, what a failure costs,
and the line it prints when it did nothing.
"""
from __future__ import annotations

import smtplib
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.utils import timezone

from openoutsend.core.agents.outreach import Email
from openoutsend.core.sequences import COLD, key
from openoutsend.emails.models import DeliveryEvent, Direction, Mailbox, Message, Thread
from openoutsend.emails.steps.send import send_step as really_send
from openoutsend.leads.models import Deal, Outcome, Suppression
from openoutsend.leads.states import COMPLETED, CONVERSING, awaiting, machine_name, sending
from openoutsend.send_pass import PassResult, _what_is_holding, run_send_pass
from openoutsend.tests.emails import maillog
from openoutsend.tests.factories import DealFactory, LeadFactory, put_in_state

pytestmark = pytest.mark.django_db


@pytest.fixture
def steps(site_config):
    """What a pass calls out to, stubbed, with the order it was called in.

    The mail pass, the measurement, the conversation's model and the step's model are
    stubs, and nothing leaves the transport. A send is the real one, recorded as
    ``open`` (a deal's first email) or ``step`` (a later one in its thread).
    """
    order: list[str] = []

    def answer(turn, prompt, entity, schema):
        order.append("answer")
        return schema.model_validate({"to": CONVERSING, "message": "Tell me more."})

    def send(deal, step, plan):
        order.append("open" if deal.thread_id is None else "step")
        return really_send(deal, step, plan)

    with patch("openoutsend.emails.mail_pass.run_mail_pass") as mail, \
            patch("openoutsend.emails.warmth.measure_pool") as measure, \
            patch("openoutsend.machines.steps.ModelTurn.ask", autospec=True) as turn, \
            patch("openoutsend.machines.steps.ModelTurn.decided_by", "llm:test:test"), \
            patch("openoutsend.emails.steps.send.send_step") as send_step, \
            patch("openoutsend.core.agents.outreach.write_email",
                  return_value=Email(subject="Hi", message="Body")), \
            patch("openoutsend.leads.summaries.materialize_profile_summary_if_missing"), \
            patch("openoutsend.emails.sender._deliver") as deliver:
        mail.side_effect = lambda: (order.append("read"), (0, 0, 0))[1]
        measure.side_effect = lambda: order.append("measure")
        turn.side_effect = answer
        send_step.side_effect = send
        yield SimpleNamespace(mail=mail, measure=measure, turn=turn, send=send_step,
                              deliver=deliver, order=order)


@pytest.fixture
def two_steps(tmp_path):
    """A two-step folder, for a deal whose later step is due, under the key a deal records."""
    folder = tmp_path / "two-steps"
    folder.mkdir()
    (folder / "E0-open.prompt.j2").write_text("Open.", encoding="utf-8")
    (folder / "E1-3d-steer.prompt.j2").write_text("Steer.", encoding="utf-8")
    return key(str(folder))


def _waiting(address="lead@acme.com", **kwargs):
    """A deal waiting for its first email, which cold's opener makes due the moment it arrives."""
    return DealFactory(lead=LeadFactory(email=address), **kwargs)


def _due(address="lead@acme.com"):
    """A deal whose opener is already due: resting in `sending_E0`."""
    return DealFactory(lead=LeadFactory(email=address), state=sending(0))


def _in_thread(box, state, address="lead@acme.com", sequence=COLD, lead=None):
    """A deal already written to from *box*, resting in *state*, its opener sent a week ago."""
    deal = DealFactory(lead=lead or LeadFactory(email=address), sequence=sequence, mailbox=box,
                       email_subject="Hi")
    deal.thread = Thread.objects.create(mailbox=box, machine=machine_name(sequence),
                                        entity=str(deal.pk))
    deal.save()
    put_in_state(deal, state)
    maillog.outbound(box, thread=deal.thread, to=deal.lead.email, step=0, subject="Hi",
                     sent_at=timezone.now() - timedelta(days=7))
    return deal


def _replied(box, address="lead@acme.com", **kwargs):
    """A deal in its conversation whose newest turn is theirs — the turn's whole trigger."""
    deal = _in_thread(box, CONVERSING, address, **kwargs)
    maillog.inbound(box, thread=deal.thread, sender=deal.lead.email)
    return deal


def _free(box):
    """Hand the opener loop *box* once, then close the pool — one send's worth."""
    return patch("openoutsend.planner.free_for_first_email", side_effect=[box, None])


def _open_window():
    return patch("openoutsend.core.sending_window.within_sending_window", return_value=True)


# ── The order ─────────────────────────────────────────────────────


def test_the_mail_is_read_before_anything_is_written(steps):
    box = maillog.mailbox()
    _replied(box)
    _waiting("other@acme.com")

    with _free(box):
        run_send_pass()

    assert steps.order == ["read", "measure", "answer", "open"]


def test_capacity_is_measured_after_the_mail_and_before_the_openers(steps):
    """The ramp's downward signal is a bounce, and a bounce arrives as ordinary mail.

    Measuring before the read would size today's volume on evidence that was sitting
    unopened in the box — the one failure the measurement exists to catch.
    """
    box = maillog.mailbox()
    _waiting()

    with _free(box):
        run_send_pass()

    assert steps.measure.call_count == 1
    assert steps.order.index("read") < steps.order.index("measure") < steps.order.index("open")


def test_the_counts_come_back_from_the_mail_pass(steps):
    steps.mail.side_effect = lambda: (4, 3, 2)

    result = run_send_pass()

    assert (result.mirrored, result.classified, result.projected) == (4, 3, 2)


# ── Ending and waiting ────────────────────────────────────────────


def test_a_deal_whose_wait_ran_out_is_made_due_and_sent_in_the_same_pass(steps):
    box = maillog.mailbox()
    deal = _waiting()
    assert deal.state == awaiting(0)

    with _free(box):
        result = run_send_pass()

    assert result.opened == 1
    assert deal.state == CONVERSING


def test_an_install_holding_no_conversations_ends_the_deal_with_its_last_send(steps, monkeypatch):
    monkeypatch.setenv("OUTSEND_CONVERSATIONS", "off")
    box = maillog.mailbox()
    deal = _waiting()

    with _free(box):
        result = run_send_pass()

    deal.refresh_from_db()
    assert result.opened == 1
    assert (deal.state, deal.outcome) == (COMPLETED, Outcome.SENT)


def test_a_reply_to_a_broadcast_is_stored_and_nobody_answers_it(steps, monkeypatch):
    monkeypatch.setenv("OUTSEND_CONVERSATIONS", "off")
    box = maillog.mailbox()
    deal = _in_thread(box, COMPLETED)
    reply = maillog.inbound(box, thread=deal.thread, sender=deal.lead.email)

    result = run_send_pass()

    assert (result.answered, result.ended, result.failed) == (0, 0, 0)
    assert not steps.turn.called
    assert deal.thread.messages.order_by("pk").last() == reply


def test_a_wait_that_ran_out_rests_as_due_when_no_box_is_free(steps):
    deal = _waiting()

    with patch("openoutsend.planner.free_for_first_email", return_value=None):
        result = run_send_pass()

    assert result.opened == 0
    assert deal.state == sending(0)


def test_a_deal_stopped_since_the_last_pass_is_ended_and_not_written_to(steps):
    """What ingest could not end: a line piped with `stop` by a run without its folder."""
    box = maillog.mailbox()
    deal = _waiting()
    Deal.objects.filter(pk=deal.pk).update(stop=True)

    with _free(box):
        result = run_send_pass()

    deal.refresh_from_db()
    steps.send.assert_not_called()
    assert (result.opened, result.failed) == (0, 0)
    assert (deal.state, deal.outcome) == (COMPLETED, Outcome.STOPPED)


def test_a_deal_whose_address_is_suppressed_is_ended_and_not_written_to(steps):
    box = maillog.mailbox()
    deal = _waiting()
    Suppression.objects.create(email="lead@acme.com")

    with _free(box):
        run_send_pass()

    deal.refresh_from_db()
    steps.send.assert_not_called()
    assert (deal.state, deal.outcome) == (COMPLETED, Outcome.UNSUBSCRIBED)


def test_a_deal_that_arrived_with_every_step_past_its_grace_is_closed_and_counted(steps):
    box = maillog.mailbox()
    deal = _waiting(start_at=timezone.now() - timedelta(days=60))

    with _free(box):
        result = run_send_pass()

    deal.refresh_from_db()
    steps.send.assert_not_called()
    assert result.missed == 1
    assert (deal.state, deal.outcome) == (COMPLETED, Outcome.MISSED)


# ── Conversing ────────────────────────────────────────────────────


def test_every_thread_that_replied_is_answered(steps):
    box = maillog.mailbox()
    first = _replied(box)
    _replied(box)

    result = run_send_pass()

    assert result.answered == 2
    assert steps.turn.call_count == 2
    answer = first.thread.messages.filter(direction=Direction.OUTBOUND).order_by("pk").last()
    assert answer.body_text == "Tell me more."
    assert first.state == CONVERSING


def test_a_turn_that_ends_the_conversation_is_saved_with_its_outcome(steps):
    box = maillog.mailbox()
    deal = _replied(box)
    steps.turn.side_effect = lambda turn, prompt, entity, schema: schema.model_validate(
        {"to": COMPLETED, "outcome": "not_interested"})

    result = run_send_pass()

    deal.refresh_from_db()
    assert (result.answered, result.ended) == (0, 1)
    assert (deal.state, deal.outcome) == (COMPLETED, Outcome.NOT_INTERESTED)
    steps.deliver.assert_not_called()


def test_a_failed_reply_costs_only_its_own_conversation(steps):
    box = maillog.mailbox()
    failing = _replied(box)
    _replied(box)
    steps.deliver.side_effect = [OSError("smtp said no"), None]

    result = run_send_pass()

    assert (result.answered, result.failed) == (1, 1)
    assert result.ok is False
    assert failing.state == CONVERSING


def test_a_conversation_gone_quiet_is_closed_and_counted(steps):
    box = maillog.mailbox()
    deal = _in_thread(box, CONVERSING)
    put_in_state(deal, CONVERSING, at=timezone.now() - timedelta(days=14))

    result = run_send_pass()

    deal.refresh_from_db()
    assert result.gave_up == 1
    assert (deal.state, deal.outcome) == (COMPLETED, Outcome.UNRESPONSIVE)
    assert steps.order == ["read", "measure"]


def test_a_conversation_is_not_closed_inside_its_closing_gap(steps):
    deal = _in_thread(maillog.mailbox(), CONVERSING)

    result = run_send_pass()

    assert result.gave_up == 0
    assert deal.state == CONVERSING


def test_a_pass_narrowed_to_a_sequence_still_answers_every_conversation(steps, two_steps):
    """Whoever wrote in is answered in whichever pass reads the mail; only the sends are narrowed."""
    box = maillog.mailbox()
    _replied(box)
    _waiting("other@acme.com")

    with _free(box):
        result = run_send_pass(sequence=two_steps)

    assert (result.answered, result.opened) == (1, 0)
    assert steps.order == ["read", "measure", "answer"]


def test_a_lead_answered_this_pass_is_sent_no_step_besides_it(steps, two_steps):
    """One message per address per pass: their other deal's opener waits for the next."""
    box = maillog.mailbox()
    replied = _replied(box)
    other = DealFactory(lead=replied.lead, sequence=two_steps, state=sending(0))

    with _open_window():
        result = run_send_pass()
        assert (result.answered, result.opened) == (1, 0)
        assert other.state == sending(0)

        result = run_send_pass()

    assert (result.answered, result.opened) == (0, 1)


# ── Opening ───────────────────────────────────────────────────────


def test_one_conversation_is_opened_per_free_box(steps):
    """The guard is the bound: the box takes itself out of the pool as it sends."""
    box = maillog.mailbox()
    _waiting()
    _waiting("second@acme.com")

    with _open_window():
        result = run_send_pass()

    assert result.opened == 1
    assert steps.send.call_count == 1


def test_a_free_box_with_nobody_waiting_opens_nothing(steps):
    box = maillog.mailbox()

    with _free(box):
        result = run_send_pass()

    assert result.opened == 0
    steps.send.assert_not_called()


def test_a_send_that_stays_put_leaves_the_state_alone(steps):
    """`False` is a send deciding not to go — a lead suppressed while the model wrote."""
    box = maillog.mailbox()
    deal = _waiting()
    steps.send.side_effect = lambda deal, step, plan: False

    with _free(box):
        result = run_send_pass()

    assert (result.opened, result.failed) == (0, 0)
    assert deal.state == sending(0)


def test_a_failed_opener_stops_the_openers_for_this_pass(steps):
    """A box that just refused a send is still 'free' — retrying would not terminate."""
    box = maillog.mailbox()
    _waiting()
    _waiting("second@acme.com")
    steps.send.side_effect = RuntimeError("smtp said no")

    with patch("openoutsend.planner.free_for_first_email", return_value=box):
        result = run_send_pass()

    assert (result.opened, result.failed) == (0, 1)
    assert steps.send.call_count == 1


def test_a_send_cut_off_before_its_commit_leaves_no_row_and_the_deal_waiting(steps):
    """The row, the send and the deal's new state commit together, after the send."""
    box = maillog.mailbox()
    deal = _due()

    with _free(box), \
            patch("agentic_fsm.backends.django.base.StateBackend.move",
                  side_effect=RuntimeError("killed")):
        result = run_send_pass()

    deal.refresh_from_db()
    assert steps.deliver.call_count == 1
    assert result.failed == 1
    assert not Message.objects.exists()
    assert not Thread.objects.exists()
    assert deal.state == sending(0)
    assert deal.thread is None


def test_a_refused_send_keeps_its_row_and_failure_through_the_rollback(steps):
    """The bounce rate counts refusals, and an undeliverable address ends its deal: the
    rollback must erase neither."""
    box = maillog.mailbox()
    deal = _waiting()
    steps.deliver.side_effect = smtplib.SMTPRecipientsRefused(
        {"lead@acme.com": (550, b"5.1.1 no such user")})

    with _free(box):
        result = run_send_pass()

    deal.refresh_from_db()
    row = Message.objects.get()
    assert (result.undeliverable, result.failed) == (1, 0)
    assert row.delivery_events.get().status != DeliveryEvent.Status.ACCEPTED
    assert (deal.state, deal.outcome) == (COMPLETED, Outcome.UNDELIVERABLE)


def test_an_undeliverable_address_does_not_stop_the_openers_behind_it(steps):
    """A dead address is about the recipient, not the box: its deal is closed and the
    next one is sent, so it cannot sit at the head of every pass."""
    box = maillog.mailbox()
    dead = _waiting("gone@acme.com")
    alive = _waiting("second@acme.com")
    refusal = smtplib.SMTPRecipientsRefused({"gone@acme.com": (550, b"5.1.1 no such user")})
    steps.deliver.side_effect = [refusal, None]

    with patch("openoutsend.planner.free_for_first_email", side_effect=[box, box, None]):
        result = run_send_pass()

    dead.refresh_from_db()
    alive.refresh_from_db()
    assert (result.opened, result.undeliverable, result.failed) == (1, 1, 0)
    assert result.ok
    assert (dead.state, dead.outcome) == (COMPLETED, Outcome.UNDELIVERABLE)
    assert alive.state == CONVERSING


def test_no_free_box_means_no_openers(steps):
    _waiting()

    with patch("openoutsend.planner.free_for_first_email", return_value=None):
        result = run_send_pass()

    assert result.opened == 0
    steps.send.assert_not_called()


# ── The line it prints ────────────────────────────────────────────


def test_the_gate_is_named_as_its_consequence_when_no_box_is_connected():
    _waiting()

    assert "no mailbox connected" in _what_is_holding(COLD)


def test_the_gate_is_named_when_the_day_is_spent():
    maillog.mailbox(daily_limit=0)

    assert "no send headroom left today" in _what_is_holding(COLD)


def test_the_gate_is_named_outside_sending_hours():
    maillog.mailbox()

    with patch("openoutsend.core.sending_window.within_sending_window", return_value=False):
        assert "outside sending hours" in _what_is_holding(COLD)


def test_the_counts_are_said_even_when_nothing_is_holding():
    maillog.mailbox(daily_limit=5)
    _waiting()

    with patch("openoutsend.core.sending_window.within_sending_window", return_value=True):
        line = _what_is_holding(COLD)

    assert "1 waiting to be emailed" in line
    assert "5 first email(s) left today" in line


def test_the_replies_owed_an_answer_are_counted_in_the_line():
    box = maillog.mailbox()
    _replied(box)
    _in_thread(box, CONVERSING, "quiet@acme.com")

    assert "0 waiting to be emailed · 1 reply(ies) to answer" in _what_is_holding(None)


def test_the_line_comes_back_on_the_result(steps):
    maillog.mailbox(daily_limit=5)
    _waiting()

    with patch("openoutsend.planner.free_for_first_email", return_value=None), _open_window():
        result = run_send_pass(narrate=False)

    assert "1 waiting to be emailed" in result.holding


# ── Later steps and openers share one queue ───────────────────────


def test_a_later_step_and_an_opener_compete_for_the_same_slot(steps, two_steps):
    """One queue, not two: a box free for one cold email, one thread whose next step is
    due and one lead waiting to be opened.

    Only one message leaves — the later step, because it has been due longer — and the
    opener waits for the box's spacing clock rather than being sent out of a reserve.
    """
    box = maillog.mailbox(daily_limit=5)
    stepping = _in_thread(box, sending(1), "silent@acme.com", sequence=two_steps)
    put_in_state(stepping, sending(1), at=timezone.now() - timedelta(days=3))
    opening = _waiting("new@acme.com")

    with _open_window():
        result = run_send_pass()

        assert steps.order == ["read", "measure", "step"]
        assert (result.stepped, result.opened) == (1, 0)
        assert (stepping.state, opening.state) == (CONVERSING, sending(0))

        Mailbox.objects.update(next_send_at=None)
        result = run_send_pass()

    assert (result.stepped, result.opened) == (0, 1)


def test_a_later_step_goes_in_its_thread_and_is_counted_apart_from_an_opener(steps, two_steps):
    box = maillog.mailbox(daily_limit=5)
    deal = _in_thread(box, sending(1), sequence=two_steps)

    with _open_window():
        result = run_send_pass()

    sent = deal.thread.messages.filter(direction=Direction.OUTBOUND).order_by("pk").last()
    assert (result.stepped, result.opened) == (1, 0)
    assert (sent.step, sent.subject, sent.body_text) == (1, "Hi", "Body")


def test_no_later_step_goes_outside_the_sending_window(steps, two_steps):
    """A later step is cold volume, so the clock speaks for it as it does for an opener."""
    box = maillog.mailbox(daily_limit=5)
    deal = _in_thread(box, sending(1), sequence=two_steps)

    with patch("openoutsend.core.sending_window.within_sending_window", return_value=False):
        result = run_send_pass()

    assert "step" not in steps.order
    assert result.stepped == 0
    assert deal.state == sending(1)


def test_an_answer_goes_outside_the_sending_window_and_past_a_spent_box(steps):
    """An answer in a thread somebody wrote in is not cold volume: no window, cap or spacing."""
    box = maillog.mailbox(daily_limit=0, next_send_at=timezone.now() + timedelta(hours=1))
    _replied(box)

    with patch("openoutsend.core.sending_window.within_sending_window", return_value=False):
        result = run_send_pass()

    assert result.answered == 1


# ── The pass does not wait ────────────────────────────────────────


class TestAPassNeverSleeps:
    """The waiting belongs to `send_job.py`, which owns the whole pass while it waits.

    Keeping it out of here is what leaves the pass firable by a timer — and what lets a
    goal wait out the *window* and the day's headroom as well, which a sleep buried in
    one step of the pass could not do without holding the mail pass hostage for hours.
    """

    def test_a_closed_pool_ends_the_pass_rather_than_waiting_for_a_box(self, steps):
        """The box takes itself out of the pool on its way past, and that ends the pass
        — whether the wait is worth taking is the job's question, asked one level up."""
        box = maillog.mailbox()
        _waiting()
        _waiting("second@acme.com")

        with _free(box):
            result = run_send_pass()

        assert result.opened == 1

    def test_the_module_has_no_clock_to_sleep_on(self):
        """A regression guard with teeth: the loop that slept in here is gone, and so is
        the `time` import it slept with."""
        import openoutsend.send_pass as module

        assert not hasattr(module, "time")


# ── The verdict ───────────────────────────────────────────────────


def test_a_pass_with_nothing_failing_is_ok():
    assert PassResult().ok is True
    assert PassResult(failed=1).ok is False
