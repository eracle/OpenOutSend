"""Fixtures every test on this side leans on.

Self-hosted means one operator, so `seller_name()` and `seller_full_name()` are a
lookup rather than a parameter — anything that renders a prompt needs a user to exist.
The `campaign` fixture pulls one in for that reason: a campaign with no operator is a
state this product does not have.
"""
import pytest

from cold_outreach.tests.factories import CampaignFactory, UserFactory


@pytest.fixture
def operator(db):
    """The one active staff user the sender runs as."""
    return UserFactory()


@pytest.fixture
def campaign(db, operator):
    """A campaign with the three things a message is written from already filled in."""
    return CampaignFactory()
