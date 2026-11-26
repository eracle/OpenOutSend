"""Factories for this side's models — the rows a send path acts on.

They came across from the finder in spirit only: a `Lead` here is what the pipe
delivered, so there is no embedding, no country and no discovery provenance to build.
"""
import factory
from django.contrib.auth.models import User
from faker import Faker

fake = Faker()


class UserFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = User

    username = factory.LazyFunction(fake.user_name)
    first_name = "Eracle"
    # The address every send is blind-copied to. Set here because a copy of your own
    # outreach is the normal case; a blank one is the exception a test states.
    email = "operator@corp.com"
    is_staff = True
    is_active = True


class LeadFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = "outsend_leads.Lead"

    lead_id = factory.Sequence(lambda n: str(n))
    linkedin_url = factory.Sequence(lambda n: f"https://www.linkedin.com/in/lead-{n}/")
    profile_text = "cto at acme, milan, 50 employees"


class DealFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = "outsend_leads.Deal"
        skip_postgeneration_save = True

    lead = factory.SubFactory(LeadFactory)

    @factory.post_generation
    def state(deal, create, extracted, **kwargs):
        """Start the deal in its sequence's machine, as ingest does, then put it in *extracted*."""
        if create:
            from openoutsend.leads.lifecycle import machine_of

            machine_of(deal).start(deal.pk)
            if extracted is not None:
                put_in_state(deal, extracted)


# ── Sequence folders and the model ──


def sequence_folder(parent, name: str, files: dict) -> str:
    """A sequence folder *name* under *parent* holding *files* (name → body), under the
    key `--sequence` gives it."""
    from openoutsend.core.sequences import key

    folder = parent / name
    folder.mkdir()
    for filename, body in files.items():
        (folder / filename).write_text(body, encoding="utf-8")
    return key(str(folder))


def model_saying(**fields):
    """The install's model, answering every call with *fields*."""
    from unittest.mock import patch

    from pydantic_ai.models.test import TestModel

    return patch("openoutsend.core.llm.get_llm_model", return_value=TestModel(custom_output_args=fields))


# ── The clock and the state ──


def let_days_pass(days: int) -> None:
    """Age everything a due time is read from, as if *days* calendar days had gone by.

    The mail log, each deal's own dates, and the machines' log and current-state rows a
    wait counts from. Every box's spacing clock is cleared with it.
    """
    from datetime import timedelta

    from agentic_fsm.backends.django.models import CurrentState, LogEntry
    from django.db.models import F

    from openoutsend.emails.models import Mailbox, Message
    from openoutsend.leads.models import Deal

    gone = timedelta(days=days)
    Message.objects.update(sent_at=F("sent_at") - gone)
    Deal.objects.update(start_at=F("start_at") - gone, created_at=F("created_at") - gone)
    LogEntry.objects.update(at=F("at") - gone)
    CurrentState.objects.update(updated_at=F("updated_at") - gone)
    Mailbox.objects.update(next_send_at=None)


def put_in_state(deal, state: str, at=None) -> None:
    """Place a started deal in *state* as of *at* (now when left out), as a test's starting point.

    Written as a row of the log, like a real move, because a wait counts from the
    newest one. No step runs: code under test moves a deal only by firing a step.
    """
    from agentic_fsm.backends.django.models import CurrentState, LogEntry
    from django.utils import timezone

    from openoutsend.leads.states import machine_name

    at = at or timezone.now()
    machine = machine_name(deal.sequence)
    CurrentState.objects.filter(machine=machine, entity=str(deal.pk)).update(state=state, updated_at=at)
    LogEntry.objects.create(machine=machine, entity=str(deal.pk), from_state=None,
                            to_state=state, decided_by="code", at=at)
