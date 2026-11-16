"""The deal machine: the graph, the moves the pass logs, and a fire with no plan in hand."""
from unittest.mock import patch

import pytest

from openoutsend.emails.models import Thread
from openoutsend.leads.lifecycle import (
    GIVE_UP,
    Plan,
    deals,
    give_up,
    load_deal,
    plan_for,
    send_opener,
)
from openoutsend.leads.models import Deal, DealState, Outcome
from openoutsend.send_pass import run_send_pass
from openoutsend.tests.emails import maillog
from openoutsend.tests.factories import DealFactory, LeadFactory

pytestmark = pytest.mark.django_db


# ── The graph ──


def test_every_state_and_step_is_on_the_graph():
    graph = deals.graph()

    assert graph.name == "deal"
    assert [state.value for state in graph.states] == ["Ready to Email", "Emailed", "Completed"]
    assert {step for _, _, step in graph.transitions} == {
        "end_suppressed", "send_opener", "close_missed",
        "answer_reply", "send_step", "follow_up", "give_up",
    }


def test_nothing_leaves_completed():
    assert not [row for row in deals.graph().transitions if row[0] == DealState.COMPLETED.value]


# ── Reading the state ──


def test_a_deal_reads_its_state_from_the_machine():
    deal = DealFactory()
    assert deal.state is DealState.READY
    assert list(Deal.objects.in_state(DealState.READY)) == [deal]
    assert not Deal.objects.in_state(DealState.EMAILED, DealState.COMPLETED).exists()


def test_a_deal_never_started_has_no_state():
    deal = Deal.objects.create(lead=LeadFactory())
    assert deal.state is None
    assert not Deal.objects.in_state(DealState.READY).exists()


# ── What the pass logs ──


def test_an_opener_sent_by_the_pass_is_logged_as_its_move():
    box = maillog.mailbox()
    deal = DealFactory(lead=LeadFactory(email="lead@acme.com"))

    def _send(deal, mailbox, step, prompt_line):
        deal.mailbox, deal.thread = mailbox, Thread.objects.create(mailbox=mailbox)
        return DealState.EMAILED

    with patch("openoutsend.emails.mail_pass.run_mail_pass", return_value=(0, 0, 0)), \
            patch("openoutsend.emails.warmth.measure_pool"), \
            patch("openoutsend.planner.free_for_first_email", side_effect=[box, None]), \
            patch("openoutsend.emails.steps.send.send_first_email", side_effect=_send):
        result = run_send_pass()

    deal.refresh_from_db()
    move = deals.history(deal.pk)[-1]
    assert result.opened == 1
    assert (deal.state, deal.mailbox) == (DealState.EMAILED, box)
    assert (move.step, move.from_state, move.to_state, move.decided_by) == \
        ("send_opener", "Ready to Email", "Emailed", "code")


def test_a_step_saves_the_deal_whether_it_moves_or_not():
    deal = DealFactory(state=DealState.EMAILED)
    deal.plan = Plan(GIVE_UP)

    deals.fire(deal.pk, give_up, entity=deal)

    deal.refresh_from_db()
    assert (deal.state, deal.outcome) == (DealState.COMPLETED, Outcome.UNRESPONSIVE)


# ── A fire with no plan in hand ──


def test_a_step_fired_without_a_plan_asks_the_planner_and_waits_when_nothing_is_owed():
    """The CLI's path: `load_deal` asks the planner, and with no box free nothing is owed,
    so the opener says "not now" and sends nothing."""
    deal = DealFactory(lead=LeadFactory(email="lead@acme.com"))

    with patch("openoutsend.planner.free_for_first_email", return_value=None), \
            patch("openoutsend.emails.steps.send.send_first_email") as send:
        assert deals.fire(deal.pk, send_opener) is None

    send.assert_not_called()
    assert deal.state is DealState.READY


def test_load_attaches_what_the_planner_would_do_now():
    box = maillog.mailbox()
    deal = DealFactory(lead=LeadFactory(email="lead@acme.com"))

    with patch("openoutsend.planner.free_for_first_email", return_value=box):
        loaded = load_deal(str(deal.pk))

    assert (loaded.plan.kind, loaded.plan.box) == ("opener", box)


def test_a_deal_owed_a_reply_is_planned_a_reply():
    box = maillog.mailbox()
    thread = Thread.objects.create(mailbox=box)
    deal = DealFactory(lead=LeadFactory(email="lead@acme.com"), state=DealState.EMAILED,
                       mailbox=box, thread=thread)
    maillog.outbound(box, thread=thread)
    maillog.inbound(box, thread=thread)

    assert plan_for(deal) == Plan("reply")
