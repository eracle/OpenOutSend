"""The list that outlives everything else on this side."""
import pytest

from cold_outreach.leads.models import Campaign, Deal, DealState, Lead, Outcome, Suppression
from cold_outreach.leads.suppression import is_suppressed, suppress_email

pytestmark = pytest.mark.django_db


@pytest.fixture
def deal():
    lead = Lead.objects.create(lead_id="7", email="anna@example.com")
    campaign = Campaign.objects.create(name="devtools")
    return Deal.objects.create(lead=lead, campaign=campaign, state=DealState.EMAILED)


def test_suppressing_ends_the_open_deals(deal):
    ended = suppress_email("anna@example.com", reason="asked to stop")

    deal.refresh_from_db()
    assert ended == 1
    assert deal.state == DealState.UNSUBSCRIBED
    assert is_suppressed("anna@example.com")


def test_it_reaches_every_campaign_holding_the_address(deal):
    second = Campaign.objects.create(name="agencies")
    other = Deal.objects.create(lead=deal.lead, campaign=second)

    assert suppress_email("anna@example.com") == 2

    other.refresh_from_db()
    assert other.state == DealState.UNSUBSCRIBED


def test_an_ended_deal_says_why_it_ended(deal):
    """However the opt-out arrived — the alias, the agent, a re-ingest — the closed
    deal carries the same reason, so the funnel does not have one ending it cannot
    account for."""
    suppress_email("anna@example.com", reason="asked to stop")

    deal.refresh_from_db()
    assert deal.outcome == Outcome.UNSUBSCRIBED


def test_a_dead_address_is_not_a_person_declining(deal):
    """`UNDELIVERABLE` is the receiver's verdict, not the lead's — it claims no outcome."""
    suppress_email("anna@example.com", reason="hard bounce",
                   end_state=DealState.UNDELIVERABLE)

    deal.refresh_from_db()
    assert deal.state == DealState.UNDELIVERABLE
    assert deal.outcome == ""


def test_a_finished_conversation_is_left_as_it_ended(deal):
    deal.state = DealState.COMPLETED
    deal.save(update_fields=["state"])

    assert suppress_email("anna@example.com") == 0

    deal.refresh_from_db()
    assert deal.state == DealState.COMPLETED


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
