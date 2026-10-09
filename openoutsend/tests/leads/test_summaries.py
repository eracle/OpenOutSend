"""The facts layer — what is extracted, when, and how often.

The LLM boundary is mocked at `_run_extraction`, which is the whole point of it being
one function: everything above it is caching, selection and persistence, and none of
that should need a model to be tested.
"""
from unittest.mock import patch

import pytest

from openoutsend.leads.summaries import facts_of, materialize_profile_summary_if_missing
from openoutsend.tests.factories import LeadFactory

pytestmark = pytest.mark.django_db


def test_the_facts_are_extracted_and_stored():
    lead = LeadFactory(profile_text="cto at acme, milan, 50 employees")

    with patch("openoutsend.leads.summaries._run_extraction",
               return_value=["Anna is CTO at Acme.", "Acme is in Milan."]) as extract:
        materialize_profile_summary_if_missing(lead)

    lead.refresh_from_db()
    assert facts_of(lead.profile_summary) == ["Anna is CTO at Acme.", "Acme is in Milan."]
    assert extract.call_args.args[1] == "cto at acme, milan, 50 employees"


def test_it_is_paid_for_once_per_lead():
    """Lazy on the way in, and never again — the firmographics do not change under us."""
    lead = LeadFactory(profile_text="cto at acme")

    with patch("openoutsend.leads.summaries._run_extraction", return_value=["A fact."]) as extract:
        materialize_profile_summary_if_missing(lead)
        materialize_profile_summary_if_missing(lead)
        materialize_profile_summary_if_missing(LeadFactory(lead_id="99", profile_summary={"facts": ["Known."]}))

    assert extract.call_count == 1


def test_a_lead_with_no_text_costs_nothing():
    lead = LeadFactory(profile_text="")

    with patch("openoutsend.leads.summaries._run_extraction") as extract:
        materialize_profile_summary_if_missing(lead)

    extract.assert_not_called()
    assert facts_of(lead.profile_summary) == []
