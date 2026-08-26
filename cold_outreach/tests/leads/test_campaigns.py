"""Which campaign an invocation is about, and what it needs before it can write."""
import pytest

from cold_outreach.errors import OutsendError
from cold_outreach.leads.campaigns import (
    DEFAULT_CAMPAIGN_NAME,
    hydrate_from_environment,
    missing_config,
    resolve_campaign,
)
from cold_outreach.leads.models import Campaign

pytestmark = pytest.mark.django_db


def test_a_fresh_install_resolves_rather_than_failing():
    """Ingest on day one must not stop the operator for a step they never knew about."""
    campaign = resolve_campaign()

    assert campaign.name == DEFAULT_CAMPAIGN_NAME


def test_the_only_campaign_needs_no_flag():
    Campaign.objects.create(name="devtools")

    assert resolve_campaign().name == "devtools"


def test_several_campaigns_are_an_error_that_lists_them():
    Campaign.objects.create(name="devtools")
    Campaign.objects.create(name="agencies")

    with pytest.raises(OutsendError) as raised:
        resolve_campaign()

    assert "devtools" in str(raised.value) and "agencies" in str(raised.value)


def test_a_named_campaign_is_created_on_first_mention():
    campaign = resolve_campaign("agencies")

    assert Campaign.objects.get(name="agencies") == campaign


def test_a_named_campaign_is_reused_on_the_second():
    first = resolve_campaign("agencies")

    assert resolve_campaign("agencies") == first
    assert Campaign.objects.count() == 1


def test_the_environment_seeds_an_empty_campaign(monkeypatch):
    monkeypatch.setenv("OUTSEND_PRODUCT_DOCS", "we sell a lead finder")
    monkeypatch.setenv("OUTSEND_CAMPAIGN_TARGET", "founders at seed-stage devtools")
    campaign = Campaign.objects.create(name="devtools")

    written = hydrate_from_environment(campaign)

    campaign.refresh_from_db()
    assert set(written) == {"product_docs", "campaign_target"}
    assert campaign.campaign_target == "founders at seed-stage devtools"
    assert missing_config(campaign) == []


def test_the_environment_never_overwrites_what_the_operator_wrote(monkeypatch):
    monkeypatch.setenv("OUTSEND_PRODUCT_DOCS", "from the environment")
    campaign = Campaign.objects.create(name="devtools", product_docs="written by hand")

    assert hydrate_from_environment(campaign) == []
    assert campaign.product_docs == "written by hand"


def test_a_booking_link_is_not_required():
    campaign = Campaign.objects.create(
        name="devtools", product_docs="a lead finder", campaign_target="founders",
    )

    assert missing_config(campaign) == []
