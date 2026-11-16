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
        """Start the deal in the deal machine, as ingest does, then put it in *extracted*."""
        if create:
            from openoutsend.leads.lifecycle import deals

            deals.start(deal.pk)
            if extracted is not None:
                put_in_state(deal, extracted)


def put_in_state(deal, state) -> None:
    """Set a started deal's current state directly, as a test's starting point.

    Not a move: no step runs and nothing is logged. Code under test moves a deal only
    by firing a step of the deal machine.
    """
    from agentic_fsm.backends.django.models import CurrentState

    from openoutsend.leads.states import MACHINE

    CurrentState.objects.filter(machine=MACHINE, entity=str(deal.pk)).update(state=state.value)
