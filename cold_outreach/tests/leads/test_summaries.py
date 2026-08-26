"""The facts layer — what is extracted, when, and how often.

The LLM boundary is mocked at `_run_extraction`, which is the whole point of it being
one function: everything above it is caching, selection and persistence, and none of
that should need a model to be tested.
"""
from unittest.mock import MagicMock, patch

import pytest

from cold_outreach.leads.summaries import (
    facts_of,
    materialize_profile_summary_if_missing,
    update_chat_summary,
)
from cold_outreach.tests.factories import DealFactory, LeadFactory

pytestmark = pytest.mark.django_db


def turn(body: str, *, outbound: bool):
    message = MagicMock()
    message.body_text = body
    message.is_outbound = outbound
    return message


# ── The lead's profile ────────────────────────────────────────────


def test_the_facts_are_extracted_and_stored():
    lead = LeadFactory(profile_text="cto at acme, milan, 50 employees")

    with patch("cold_outreach.leads.summaries._run_extraction",
               return_value=["Anna is CTO at Acme.", "Acme is in Milan."]) as extract:
        materialize_profile_summary_if_missing(lead)

    lead.refresh_from_db()
    assert facts_of(lead.profile_summary) == ["Anna is CTO at Acme.", "Acme is in Milan."]
    assert extract.call_args.args[1] == "cto at acme, milan, 50 employees"


def test_it_is_paid_for_once_per_lead():
    """Lazy on the way in, and never again — the firmographics do not change under us."""
    lead = LeadFactory(profile_text="cto at acme")

    with patch("cold_outreach.leads.summaries._run_extraction", return_value=["A fact."]) as extract:
        materialize_profile_summary_if_missing(lead)
        materialize_profile_summary_if_missing(lead)
        materialize_profile_summary_if_missing(LeadFactory(lead_id="99", profile_summary={"facts": ["Known."]}))

    assert extract.call_count == 1


def test_a_lead_with_no_text_costs_nothing():
    lead = LeadFactory(profile_text="")

    with patch("cold_outreach.leads.summaries._run_extraction") as extract:
        materialize_profile_summary_if_missing(lead)

    extract.assert_not_called()
    assert facts_of(lead.profile_summary) == []


# ── The conversation ──────────────────────────────────────────────


def test_new_turns_are_folded_into_what_is_known():
    deal = DealFactory(chat_summary={"facts": ["Lead is curious about pricing."]})

    with patch("cold_outreach.leads.summaries._run_extraction",
               return_value=["Lead is curious about pricing.", "Lead has a team of four."]) as extract:
        update_chat_summary(deal, [turn("How much is it?", outbound=False)], seller_name="Eracle")

    deal.refresh_from_db()
    assert facts_of(deal.chat_summary) == ["Lead is curious about pricing.", "Lead has a team of four."]

    system, transcript = extract.call_args.args
    assert "Eracle" in system, "the identity binding has to reach the model"
    assert "Lead is curious about pricing." in transcript, "what is known is part of the input"
    assert "[Lead]: How much is it?" in transcript


def test_our_own_messages_are_context_not_facts():
    deal = DealFactory()

    with patch("cold_outreach.leads.summaries._run_extraction", return_value=[]) as extract:
        update_chat_summary(
            deal,
            [turn("How do you handle this today?", outbound=True), turn("Badly.", outbound=False)],
            seller_name="Eracle",
        )

    transcript = extract.call_args.args[1]
    assert "[Me]: How do you handle this today?" in transcript
    assert "[Lead]: Badly." in transcript


def test_nothing_new_is_not_a_call():
    deal = DealFactory(chat_summary={"facts": ["Known."]})

    with patch("cold_outreach.leads.summaries._run_extraction") as extract:
        update_chat_summary(deal, [], seller_name="Eracle")

    extract.assert_not_called()
    deal.refresh_from_db()
    assert facts_of(deal.chat_summary) == ["Known."]
