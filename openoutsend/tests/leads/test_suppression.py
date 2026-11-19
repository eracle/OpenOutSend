"""The list that outlives everything else on this side."""
import logging
from unittest.mock import patch

import pytest

from openoutsend.core import sequences
from openoutsend.leads.lifecycle import machine_of
from openoutsend.leads.models import Lead, Outcome, Suppression
from openoutsend.leads.states import COMPLETED, CONVERSING, awaiting, sending
from openoutsend.leads.suppression import is_suppressed, suppress_email
from openoutsend.tests.factories import DealFactory, put_in_state

pytestmark = pytest.mark.django_db


@pytest.fixture
def deal():
    lead = Lead.objects.create(lead_id="7", email="anna@example.com")
    return DealFactory(lead=lead, state=CONVERSING)


@pytest.fixture
def relaunch(tmp_path):
    """A second sequence folder, under the key `--sequence` gives it."""
    folder = tmp_path / "relaunch"
    folder.mkdir()
    (folder / "E0-a.prompt.j2").write_text("Write to them.", encoding="utf-8")
    return sequences.key(str(folder))


def test_suppressing_ends_the_open_deals(deal):
    ended = suppress_email("anna@example.com", reason="asked to stop")

    deal.refresh_from_db()
    assert ended == 1
    assert deal.state == COMPLETED
    assert is_suppressed("anna@example.com")


@pytest.mark.parametrize("state", [awaiting(0), sending(0), CONVERSING])
def test_a_deal_is_ended_from_wherever_it_is_in_its_sequence(deal, state):
    put_in_state(deal, state)

    assert suppress_email("anna@example.com") == 1

    deal.refresh_from_db()
    assert (deal.state, deal.outcome) == (COMPLETED, Outcome.UNSUBSCRIBED)


def test_each_deal_is_ended_by_its_own_logged_move(deal):
    suppress_email("anna@example.com", reason="asked to stop")

    move = machine_of(deal).history(deal.pk)[-1]
    assert (move.decided_by, move.from_state, move.to_state) == ("code", "conversing", "completed")


def test_it_reaches_every_lead_holding_the_address(deal):
    other_lead = Lead.objects.create(lead_id="8", email="anna@example.com")
    other = DealFactory(lead=other_lead)

    assert suppress_email("anna@example.com") == 2

    other.refresh_from_db()
    assert (other.state, other.outcome) == (COMPLETED, Outcome.UNSUBSCRIBED)


def test_it_reaches_the_leads_deal_in_every_sequence(deal, relaunch):
    other = DealFactory(lead=deal.lead, sequence=relaunch)

    assert suppress_email("anna@example.com") == 2

    other.refresh_from_db()
    assert (other.state, other.outcome) == (COMPLETED, Outcome.UNSUBSCRIBED)
    assert machine_of(other).history(other.pk)[-1].decided_by == "code"


def test_an_ended_deal_says_why_it_ended(deal):
    """However the opt-out arrived — the alias, the agent, a re-ingest — the closed
    deal carries the same reason, so the funnel does not have one ending it cannot
    account for."""
    suppress_email("anna@example.com", reason="asked to stop")

    deal.refresh_from_db()
    assert deal.outcome == Outcome.UNSUBSCRIBED


def test_a_dead_address_is_not_a_person_declining(deal):
    """Both endings are `completed` and neither is sendable; the outcome is the
    difference, so a dead mailbox never reads as withdrawn consent."""
    suppress_email("anna@example.com", reason="hard bounce",
                   outcome=Outcome.UNDELIVERABLE)

    deal.refresh_from_db()
    assert deal.state == COMPLETED
    assert deal.outcome == Outcome.UNDELIVERABLE


def test_a_finished_conversation_is_left_as_it_ended(deal):
    """Including the verdict it ended with: an opt-out arriving after a conversation
    closed changes nothing about how it closed."""
    put_in_state(deal, COMPLETED)
    deal.outcome = Outcome.NOT_INTERESTED
    deal.save(update_fields=["outcome"])

    assert suppress_email("anna@example.com") == 0

    deal.refresh_from_db()
    assert (deal.state, deal.outcome) == (COMPLETED, Outcome.NOT_INTERESTED)
    assert is_suppressed("anna@example.com")


def test_addresses_compare_case_insensitively(deal):
    suppress_email("  ANNA@Example.com ")

    assert is_suppressed("anna@example.com")
    assert Suppression.objects.get().email == "anna@example.com"


def test_re_suppressing_keeps_the_first_answer(deal):
    suppress_email("anna@example.com", reason="the first time they asked")
    suppress_email("anna@example.com", reason="a second opt-out later")

    assert Suppression.objects.count() == 1
    assert Suppression.objects.get().reason == "the first time they asked"


def test_a_blank_address_is_not_a_suppression():
    """A row with no address has nothing to check — which is why send checks again."""
    assert suppress_email("") == 0
    assert suppress_email(None) == 0
    assert not is_suppressed("")
    assert not Suppression.objects.exists()


# ── The deal whose own step is suppressing ──


def test_the_deal_that_is_leaving_is_not_ended_by_the_suppression(deal, relaunch):
    """Its own step ends it by its own move, so it is not ended a second time here."""
    other = DealFactory(lead=deal.lead, sequence=relaunch)
    moves_before = len(machine_of(deal).history(deal.pk))

    ended = suppress_email("anna@example.com", reason="worded unsubscribe in a reply", leaving=deal)

    deal.refresh_from_db()
    other.refresh_from_db()
    assert ended == 1
    assert (deal.state, deal.outcome) == (CONVERSING, "")
    assert len(machine_of(deal).history(deal.pk)) == moves_before
    assert (other.state, other.outcome) == (COMPLETED, Outcome.UNSUBSCRIBED)
    assert is_suppressed("anna@example.com")


# ── A deal whose folder this run was not handed ──


def test_a_deal_whose_sequence_folder_cannot_be_found_is_left_open_and_said_so(
        deal, relaunch, tmp_path, caplog):
    """The address is on the list, so nothing can be sent to it; the deal is ended by the
    next pass that has its folder."""
    stranded = DealFactory(lead=deal.lead, sequence=relaunch)
    sequences._FOLDERS.clear()  # a later run, which was not handed the folder

    with patch.object(sequences, "operator_dir", return_value=tmp_path / "none-kept-here"), \
            caplog.at_level(logging.ERROR, logger="openoutsend.leads.suppression"):
        ended = suppress_email("anna@example.com", reason="asked to stop")

    deal.refresh_from_db()
    assert ended == 1
    assert is_suppressed("anna@example.com")
    assert (deal.state, deal.outcome) == (COMPLETED, Outcome.UNSUBSCRIBED)
    stranded.refresh_from_db()
    assert (stranded.state, stranded.outcome) == (awaiting(0), "")
    (complaint,) = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert "anna@example.com is suppressed" in complaint
    assert "'relaunch' stays open" in complaint and "--sequence" in complaint
