"""What the pipe promises, asserted line by line.

Every test here is a row of the boundary contract's table. They read from a string
stream rather than a real pipe, because the thing under test is what the bytes become
— the transport is `sys.stdin` and has nothing to prove.
"""
import io
import json

import pytest

from cold_outreach.leads.ingest import ingest
from cold_outreach.leads.models import Campaign, Deal, DealState, Lead, Outcome
from cold_outreach.leads.suppression import suppress_email

pytestmark = pytest.mark.django_db


def record(**overrides) -> dict:
    """One export record, with the ten fields plus `profile_text`."""
    return {
        "email": "anna@example.com",
        "first_name": "Anna",
        "last_name": "Rossi",
        "company": "Acme",
        "title": "CTO",
        "website": "acme.io",
        "linkedin_url": "https://example.test/in/anna",
        "reason": "runs the platform team at a company in our target band",
        "lead_id": 7,
        "qualified_at": "2026-08-20T09:00:00+00:00",
        "profile_text": "cto at acme, milan, 50 employees",
        **overrides,
    }


def stream(*records) -> io.StringIO:
    """Those records as JSON Lines."""
    return io.StringIO("".join(json.dumps(r) + "\n" for r in records))


@pytest.fixture
def campaign():
    return Campaign.objects.create(name="devtools")


def test_a_record_becomes_a_lead_and_a_deal(campaign):
    result = ingest(stream(record()), campaign)

    assert (result.stored, result.skipped, result.suppressed) == (1, 0, 0)
    assert result.ok
    lead = Lead.objects.get(lead_id="7")
    assert lead.email == "anna@example.com"
    assert lead.profile_text == "cto at acme, milan, 50 employees"
    deal = Deal.objects.get(lead=lead, campaign=campaign)
    assert deal.state == DealState.READY
    assert deal.reason.startswith("runs the platform team")
    assert deal.qualified_at.isoformat() == "2026-08-20T09:00:00+00:00"


def test_re_ingesting_the_same_row_changes_nothing(campaign):
    ingest(stream(record()), campaign)
    ingest(stream(record()), campaign)

    assert Lead.objects.count() == 1
    assert Deal.objects.count() == 1


def test_the_same_person_in_two_campaigns_keeps_two_reasons(campaign):
    other = Campaign.objects.create(name="agencies")

    ingest(stream(record()), campaign)
    ingest(stream(record(reason="different verdict here")), other)

    assert Lead.objects.count() == 1
    assert Deal.objects.count() == 2
    assert Deal.objects.get(campaign=campaign).reason.startswith("runs the platform team")
    assert Deal.objects.get(campaign=other).reason == "different verdict here"


def test_latest_wins_on_a_correction(campaign):
    ingest(stream(record()), campaign)
    ingest(stream(record(reason="re-qualified under a changed ICP", title="VP Engineering")), campaign)

    assert Lead.objects.get(lead_id="7").title == "VP Engineering"
    assert Deal.objects.get().reason == "re-qualified under a changed ICP"


def test_a_later_enrichment_fills_the_blank_address(campaign):
    ingest(stream(record(email="")), campaign)
    assert Lead.objects.get(lead_id="7").email == ""

    ingest(stream(record(email="anna@acme.io")), campaign)
    assert Lead.objects.get(lead_id="7").email == "anna@acme.io"


def test_a_blank_field_never_overwrites_a_stored_one(campaign):
    """`null` in a record means *never told*, which is not a correction of anything."""
    ingest(stream(record()), campaign)
    ingest(stream(record(email=None, first_name=None, title="")), campaign)

    lead = Lead.objects.get(lead_id="7")
    assert (lead.email, lead.first_name, lead.title) == ("anna@example.com", "Anna", "CTO")


def test_a_row_with_no_address_is_stored_not_rejected(campaign):
    result = ingest(stream(record(email=None)), campaign)

    assert result.stored == 1
    assert Deal.objects.get().state == DealState.READY


def test_a_malformed_line_is_skipped_and_the_others_survive(campaign, caplog):
    body = json.dumps(record()) + "\nnot json at all\n" + json.dumps(record(lead_id=8)) + "\n"

    result = ingest(io.StringIO(body), campaign)

    assert (result.stored, result.skipped) == (2, 1)
    assert not result.ok, "a skipped line has to reach the exit code"
    assert "line 2 skipped" in caplog.text


def test_a_record_with_no_lead_id_is_malformed(campaign):
    """The key is what makes a re-ingest idempotent; a row without one duplicates forever."""
    result = ingest(stream(record(lead_id=None)), campaign)

    assert (result.stored, result.skipped) == (0, 1)
    assert not Lead.objects.exists()


def test_unknown_keys_are_kept_rather_than_refused(campaign):
    """The compatibility rule: the finder only ever adds, and a receiver ignores what it does not know."""
    result = ingest(stream(record(some_future_field="whatever this turns out to be")), campaign)

    assert result.stored == 1


def test_blank_lines_are_not_rows(campaign):
    result = ingest(io.StringIO("\n\n" + json.dumps(record()) + "\n\n"), campaign)

    assert (result.stored, result.skipped) == (1, 0)


# ── The door ──────────────────────────────────────────────────────


def test_a_suppressed_address_arrives_unsendable(campaign):
    suppress_email("anna@example.com", reason="opted out last week")

    result = ingest(stream(record()), campaign)

    assert (result.stored, result.suppressed) == (1, 1)
    assert Deal.objects.get().outcome == Outcome.UNSUBSCRIBED


def test_a_re_ingest_never_resurrects_an_opt_out(campaign):
    ingest(stream(record()), campaign)
    suppress_email("anna@example.com", reason="asked to stop")
    assert Deal.objects.get().outcome == Outcome.UNSUBSCRIBED

    ingest(stream(record()), campaign)

    assert Deal.objects.get().outcome == Outcome.UNSUBSCRIBED


def test_an_address_that_changes_into_a_suppressed_one_is_caught(campaign):
    """Ingest is lead-keyed, suppression is address-keyed — so a corrected address is re-checked."""
    suppress_email("anna@acme.io", reason="opted out under her work address")
    ingest(stream(record()), campaign)
    assert Deal.objects.get().state == DealState.READY

    ingest(stream(record(email="anna@acme.io")), campaign)

    assert Deal.objects.get().outcome == Outcome.UNSUBSCRIBED


def test_an_opt_out_ends_the_conversation_it_was_in(campaign):
    ingest(stream(record()), campaign)
    deal = Deal.objects.get()
    deal.state = DealState.EMAILED
    deal.save(update_fields=["state"])

    suppress_email("Anna@Example.com  ", reason="worded unsubscribe")

    deal.refresh_from_db()
    assert (deal.state, deal.outcome) == (DealState.COMPLETED, Outcome.UNSUBSCRIBED)
