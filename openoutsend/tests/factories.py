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

    lead = factory.SubFactory(LeadFactory)
