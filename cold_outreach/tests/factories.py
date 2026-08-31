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


class SiteConfigFactory(factory.django.DjangoModelFactory):
    """Sets fields on the one `SiteConfig` row, creating it if it does not exist yet.

    A singleton can't be `get_or_create`d by field values the way an ordinary model
    can — a row from an earlier fixture in the same test (`stored_llm`, an autouse
    `SiteConfig.load()`) already occupies `pk=1`, and `get_or_create` would return it
    untouched rather than applying these defaults. `_create` loads-and-updates instead.
    """

    class Meta:
        model = "outsend_core.SiteConfig"

    product_docs = "A self-hosted lead finder that writes down why each lead fits."
    campaign_target = "Founders and heads of growth at small B2B software companies."

    @classmethod
    def _create(cls, model_class, *args, **kwargs):
        config = model_class.load()
        for field, value in kwargs.items():
            setattr(config, field, value)
        config.save()
        return config


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
