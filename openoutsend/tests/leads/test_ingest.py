"""What the pipe promises, asserted line by line.

Every test here is a row of the boundary contract's table. They read from a string
stream rather than a real pipe, because the thing under test is what the bytes become
— the transport is `sys.stdin` and has nothing to prove.
"""
import io
import json
from unittest.mock import patch

import pytest
from django.utils import timezone

from openoutsend.core.sequences import SequenceError, key
from openoutsend.leads.ingest import ingest
from openoutsend.leads.lifecycle import machine_of
from openoutsend.leads.models import Deal, Lead, Outcome
from openoutsend.leads.states import COMPLETED, CONVERSING, awaiting, sending
from openoutsend.leads.suppression import suppress_email
from openoutsend.tests.factories import put_in_state

pytestmark = pytest.mark.django_db


@pytest.fixture
def relaunch(tmp_path):
    """A second sequence folder, under the key `--sequence` gives it."""
    folder = tmp_path / "relaunch"
    folder.mkdir()
    (folder / "E0-a.prompt.j2").write_text("Write to them.", encoding="utf-8")
    (folder / "E1-2d-b.prompt.j2").write_text("Write again.", encoding="utf-8")
    return key(str(folder))


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


def test_a_record_becomes_a_lead_and_a_deal():
    result = ingest(stream(record()))

    assert (result.stored, result.skipped, result.suppressed) == (1, 0, 0)
    assert result.ok
    lead = Lead.objects.get(lead_id="7")
    assert lead.email == "anna@example.com"
    assert lead.profile_text == "cto at acme, milan, 50 employees"
    deal = Deal.objects.get(lead=lead)
    assert deal.state == awaiting(0)
    assert [(m.step, m.to_state) for m in machine_of(deal).history(deal.pk)] == [(None, "awaiting_E0")]
    assert deal.reason.startswith("runs the platform team")
    assert deal.qualified_at.isoformat() == "2026-08-20T09:00:00+00:00"


def test_re_ingesting_the_same_row_changes_nothing():
    ingest(stream(record()))
    ingest(stream(record()))

    assert Lead.objects.count() == 1
    assert Deal.objects.count() == 1


def test_a_lead_lands_in_cold_unless_told_otherwise():
    ingest(stream(record()))

    assert Deal.objects.get().sequence == "cold"


def test_the_same_lead_in_another_sequence_gets_a_deal_of_its_own(relaunch):
    ingest(stream(record()))
    ingest(stream(record()), sequence=relaunch)
    ingest(stream(record()), sequence=relaunch)

    assert Lead.objects.count() == 1
    assert sorted(Deal.objects.values_list("sequence", flat=True)) == ["cold", "relaunch"]


def test_each_deal_is_started_in_its_own_sequences_machine(relaunch):
    ingest(stream(record()))
    ingest(stream(record()), sequence=relaunch)

    cold, other = Deal.objects.get(sequence="cold"), Deal.objects.get(sequence=relaunch)
    assert (machine_of(cold).name, machine_of(other).name) == ("deal-cold", "deal-relaunch")
    assert (cold.state, other.state) == (awaiting(0), awaiting(0))
    assert machine_of(cold).state_of(other.pk) is None


def test_a_new_sequence_does_not_reopen_a_finished_deal_in_another(relaunch):
    ingest(stream(record()))
    put_in_state(Deal.objects.get(), COMPLETED)
    Deal.objects.update(outcome=Outcome.NOT_INTERESTED)

    ingest(stream(record()), sequence=relaunch)

    finished = Deal.objects.get(sequence="cold")
    assert (finished.state, finished.outcome) == (COMPLETED, Outcome.NOT_INTERESTED)
    assert Deal.objects.get(sequence=relaunch).state == awaiting(0)


def test_a_re_ingest_never_moves_a_deal_back_to_its_start():
    """Where a conversation is in its sequence is this side's own business."""
    ingest(stream(record()))
    put_in_state(Deal.objects.get(), CONVERSING)

    ingest(stream(record(reason="re-qualified under a changed ICP")))

    deal = Deal.objects.get()
    assert (deal.state, deal.reason) == (CONVERSING, "re-qualified under a changed ICP")


def test_a_sequence_with_no_folder_stores_no_deal(tmp_path):
    """A deal has no state without its folder's machine, so none is made without one."""
    from openoutsend.core import sequences

    with patch.object(sequences, "operator_dir", return_value=tmp_path), \
            pytest.raises(SequenceError, match="no folder of that name"):
        ingest(stream(record()), sequence="nowhere")

    assert not Deal.objects.exists()


def test_latest_wins_on_a_correction():
    ingest(stream(record()))
    ingest(stream(record(reason="re-qualified under a changed ICP", title="VP Engineering")))

    assert Lead.objects.get(lead_id="7").title == "VP Engineering"
    assert Deal.objects.get().reason == "re-qualified under a changed ICP"


def test_a_later_enrichment_fills_the_blank_address():
    ingest(stream(record(email="")))
    assert Lead.objects.get(lead_id="7").email == ""

    ingest(stream(record(email="anna@acme.io")))
    assert Lead.objects.get(lead_id="7").email == "anna@acme.io"


def test_a_blank_field_never_overwrites_a_stored_one():
    """`null` in a record means *never told*, which is not a correction of anything."""
    ingest(stream(record()))
    ingest(stream(record(email=None, first_name=None, title="")))

    lead = Lead.objects.get(lead_id="7")
    assert (lead.email, lead.first_name, lead.title) == ("anna@example.com", "Anna", "CTO")


def test_a_row_with_no_address_is_stored_not_rejected():
    result = ingest(stream(record(email=None)))

    assert result.stored == 1
    assert Deal.objects.get().state == awaiting(0)


def test_a_malformed_line_is_skipped_and_the_others_survive(caplog):
    body = json.dumps(record()) + "\nnot json at all\n" + json.dumps(record(lead_id=8)) + "\n"

    result = ingest(io.StringIO(body))

    assert (result.stored, result.skipped) == (2, 1)
    assert not result.ok, "a skipped line has to reach the exit code"
    assert "line 2 skipped" in caplog.text


def test_a_record_with_no_lead_id_is_malformed():
    """The key is what makes a re-ingest idempotent; a row without one duplicates forever."""
    result = ingest(stream(record(lead_id=None)))

    assert (result.stored, result.skipped) == (0, 1)
    assert not Lead.objects.exists()


def test_unknown_keys_are_kept_rather_than_refused():
    """The compatibility rule: the finder only ever adds, and a receiver ignores what it does not know."""
    result = ingest(stream(record(some_future_field="whatever this turns out to be")))

    assert result.stored == 1


def test_blank_lines_are_not_rows():
    result = ingest(io.StringIO("\n\n" + json.dumps(record()) + "\n\n"))

    assert (result.stored, result.skipped) == (1, 0)


# ── The sequence's own fields ─────────────────────────────────────


def test_the_other_keys_become_the_deals_prompt_fields():
    ingest(stream(record(order_url="https://pay.example/1", leads=[{"name": "Bo"}])))

    assert Deal.objects.get().fields == {"order_url": "https://pay.example/1", "leads": [{"name": "Bo"}]}


def test_prompt_fields_merge_latest_wins_key_by_key():
    ingest(stream(record(order_url="https://pay.example/1", product="audits")))
    ingest(stream(record(order_url="https://pay.example/2", product=None, buyer="")))

    assert Deal.objects.get().fields == {"order_url": "https://pay.example/2", "product": "audits"}


def test_start_at_is_the_ingest_time_when_the_line_has_none():
    before = timezone.now()
    ingest(stream(record()))

    assert before <= Deal.objects.get().start_at <= timezone.now()


def test_start_at_is_read_from_the_line_and_kept_when_a_later_one_omits_it():
    ingest(stream(record(start_at="2026-10-07T09:12:00Z")))
    ingest(stream(record()))

    deal = Deal.objects.get()
    assert deal.start_at.isoformat() == "2026-10-07T09:12:00+00:00"
    assert "start_at" not in deal.fields


def test_a_start_at_with_no_offset_is_utc():
    ingest(stream(record(start_at="2026-10-07T09:12:00")))

    assert Deal.objects.get().start_at.isoformat() == "2026-10-07T09:12:00+00:00"


def test_a_re_export_with_stop_lands_on_the_same_deal():
    ingest(stream(record()))
    assert Deal.objects.get().stop is False

    ingest(stream(record(stop=True)))

    deal = Deal.objects.get()
    assert deal.stop is True
    assert "stop" not in deal.fields


# ── A line piped with `stop` ──────────────────────────────────────


def test_a_line_arriving_with_stop_ends_its_deal_stopped():
    result = ingest(stream(record(stop=True)))

    deal = Deal.objects.get()
    assert (result.stored, result.suppressed) == (1, 0)
    assert (deal.state, deal.outcome) == (COMPLETED, Outcome.STOPPED)
    assert [(m.step, m.to_state) for m in machine_of(deal).history(deal.pk)] == \
        [(None, "awaiting_E0"), ("end_suppressed", "completed")]


@pytest.mark.parametrize("state", [awaiting(0), sending(0), CONVERSING])
def test_a_re_ingest_with_stop_ends_an_open_deal_wherever_it_is(state):
    ingest(stream(record()))
    put_in_state(Deal.objects.get(), state)

    ingest(stream(record(stop=True)))

    deal = Deal.objects.get()
    move = machine_of(deal).history(deal.pk)[-1]
    assert (deal.state, deal.outcome) == (COMPLETED, Outcome.STOPPED)
    assert (move.step, move.from_state, move.to_state) == ("end_suppressed", state, "completed")


def test_stop_ends_the_deal_of_the_sequence_it_was_piped_into_and_no_other(relaunch):
    ingest(stream(record()))
    ingest(stream(record()), sequence=relaunch)

    ingest(stream(record(stop=True)), sequence=relaunch)

    stopped, other = Deal.objects.get(sequence=relaunch), Deal.objects.get(sequence="cold")
    assert (stopped.state, stopped.outcome) == (COMPLETED, Outcome.STOPPED)
    assert (other.state, other.outcome) == (awaiting(0), "")


def test_a_stopped_deal_is_not_ended_twice_and_a_later_line_does_not_reopen_it():
    ingest(stream(record(stop=True)))
    ingest(stream(record(stop=True)))
    ingest(stream(record(stop=False)))

    deal = Deal.objects.get()
    assert (deal.state, deal.outcome, deal.stop) == (COMPLETED, Outcome.STOPPED, False)
    assert len(machine_of(deal).history(deal.pk)) == 2


def test_stop_does_not_change_how_a_finished_deal_ended():
    ingest(stream(record()))
    put_in_state(Deal.objects.get(), COMPLETED)
    Deal.objects.update(outcome=Outcome.CONVERTED)

    ingest(stream(record(stop=True)))

    deal = Deal.objects.get()
    assert (deal.state, deal.outcome) == (COMPLETED, Outcome.CONVERTED)


def test_a_suppressed_line_piped_with_stop_still_ends_unsubscribed():
    suppress_email("anna@example.com", reason="opted out last week")

    result = ingest(stream(record(stop=True)))

    deal = Deal.objects.get()
    assert result.suppressed == 1
    assert (deal.state, deal.outcome) == (COMPLETED, Outcome.UNSUBSCRIBED)


@pytest.mark.parametrize("bad", [{"start_at": "next tuesday"}, {"start_at": 5}, {"stop": "yes"}])
def test_an_engine_field_it_cannot_read_makes_the_line_malformed(bad, caplog):
    result = ingest(stream(record(**bad)))

    assert (result.stored, result.skipped) == (0, 1)
    assert "line 1 skipped" in caplog.text


# ── The lead's timezone ───────────────────────────────────────────


def test_a_zone_name_is_stored_on_the_lead_and_is_not_a_prompt_field():
    ingest(stream(record(timezone="America/Chicago")))

    assert Lead.objects.get().timezone == "America/Chicago"
    assert "timezone" not in Deal.objects.get().fields


def test_a_line_with_no_zone_leaves_it_blank():
    ingest(stream(record()))

    assert Lead.objects.get().timezone == ""


def test_a_re_ingest_carrying_a_zone_corrects_it_and_a_blank_one_keeps_it():
    ingest(stream(record(timezone="America/Chicago")))
    ingest(stream(record(timezone="America/New_York")))
    ingest(stream(record(timezone="")))

    assert Lead.objects.get().timezone == "America/New_York"


@pytest.mark.parametrize("bad", ["US", "Mars/Olympus", "+05:30", 5])
def test_a_zone_the_tz_database_cannot_load_makes_the_line_malformed(bad, caplog):
    """A country is not a zone: nothing on this side turns one into the other."""
    result = ingest(stream(record(timezone=bad)))

    assert (result.stored, result.skipped) == (0, 1)
    assert "is not an IANA zone name" in caplog.text


# ── The door ──────────────────────────────────────────────────────


def test_a_suppressed_address_arrives_unsendable():
    suppress_email("anna@example.com", reason="opted out last week")

    result = ingest(stream(record()))

    assert (result.stored, result.suppressed) == (1, 1)
    deal = Deal.objects.get()
    assert (deal.state, deal.outcome) == (COMPLETED, Outcome.UNSUBSCRIBED)
    assert [(m.step, m.to_state) for m in machine_of(deal).history(deal.pk)] == \
        [(None, "awaiting_E0"), ("end_suppressed", "completed")]


def test_an_opt_out_follows_the_lead_into_a_new_sequence(relaunch):
    ingest(stream(record()))
    suppress_email("anna@example.com", reason="asked to stop")

    result = ingest(stream(record()), sequence=relaunch)

    assert result.suppressed == 1
    followed = Deal.objects.get(sequence=relaunch)
    assert (followed.state, followed.outcome) == (COMPLETED, Outcome.UNSUBSCRIBED)


def test_a_re_ingest_never_resurrects_an_opt_out():
    ingest(stream(record()))
    suppress_email("anna@example.com", reason="asked to stop")
    assert Deal.objects.get().outcome == Outcome.UNSUBSCRIBED

    ingest(stream(record()))

    assert Deal.objects.get().outcome == Outcome.UNSUBSCRIBED


def test_an_address_that_changes_into_a_suppressed_one_is_caught():
    """Ingest is lead-keyed, suppression is address-keyed — so a corrected address is re-checked."""
    suppress_email("anna@acme.io", reason="opted out under her work address")
    ingest(stream(record()))
    assert Deal.objects.get().state == awaiting(0)

    ingest(stream(record(email="anna@acme.io")))

    deal = Deal.objects.get()
    assert (deal.state, deal.outcome) == (COMPLETED, Outcome.UNSUBSCRIBED)


def test_an_opt_out_ends_the_conversation_it_was_in():
    ingest(stream(record()))
    deal = Deal.objects.get()
    put_in_state(deal, CONVERSING)

    suppress_email("Anna@Example.com  ", reason="worded unsubscribe")

    deal.refresh_from_db()
    assert (deal.state, deal.outcome) == (COMPLETED, Outcome.UNSUBSCRIBED)


# ── A resend install ──────────────────────────────────────────────


def test_a_resend_install_refuses_a_line_it_could_never_send(monkeypatch):
    """No mailbox is read, so the line's own link is the only opt-out there is."""
    monkeypatch.setenv("OUTSEND_TRANSPORT", "resend")

    result = ingest(stream(record()))

    assert (result.stored, result.skipped) == (0, 1)
    assert not Deal.objects.exists()


def test_a_resend_install_keeps_the_link_as_a_field(monkeypatch):
    monkeypatch.setenv("OUTSEND_TRANSPORT", "resend")

    ingest(stream(record(unsubscribe_url="https://hub.test/unsubscribe/t/")))

    assert Deal.objects.get().fields["unsubscribe_url"] == "https://hub.test/unsubscribe/t/"


def test_a_stop_line_needs_no_link(monkeypatch):
    monkeypatch.setenv("OUTSEND_TRANSPORT", "resend")

    result = ingest(stream(record(stop=True)))

    deal = Deal.objects.get()
    assert result.stored == 1
    assert (deal.stop, deal.state, deal.outcome) == (True, COMPLETED, Outcome.STOPPED)


def test_a_mailbox_install_takes_a_line_without_a_link(monkeypatch):
    monkeypatch.delenv("OUTSEND_TRANSPORT", raising=False)

    assert ingest(stream(record())).stored == 1
