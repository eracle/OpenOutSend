"""Fixtures every test on this side leans on.

Self-hosted means one operator, so `seller_name()` and `seller_full_name()` are a
lookup rather than a parameter — anything that renders a prompt needs a user to exist.
The `site_config` fixture pulls one in for that reason: a message with no operator is
a state this product does not have.
"""
import pytest

from cold_outreach.tests.factories import SiteConfigFactory, UserFactory


@pytest.fixture
def operator(db):
    """The one active staff user the sender runs as."""
    return UserFactory()


@pytest.fixture
def site_config(db, operator):
    """A `SiteConfig` with the three things a message is written from already filled in."""
    return SiteConfigFactory()
