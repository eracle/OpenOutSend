"""Fixtures every test on this side leans on.

Self-hosted means one operator, so `seller_name()` and `seller_full_name()` are a
lookup rather than a parameter — anything that renders a prompt needs a user to exist.
The `site_config` fixture pulls one in for that reason: a message with no operator is
a state this product does not have.

**Config is the environment, so configuring a test means setting variables.** There is
no row to build and no factory for one — `site_config` puts the two required
`OUTSEND_*` values in scope and hands back what the code will read.
"""
from unittest.mock import patch

import pytest

from cold_outreach.core.config import MESSAGE_ENV, SiteConfig
from cold_outreach.tests.factories import UserFactory

PRODUCT_DOCS = "A self-hosted lead finder that writes down why each lead fits."
CAMPAIGN_TARGET = "Founders and heads of growth at small B2B software companies."


@pytest.fixture(autouse=True)
def never_ping_a_real_provider():
    """The model check runs on every pass now, so an unpatched test would hit the network.

    Autouse and global: reaching a provider from a test suite is a bug wherever it
    happens, and the tests that are *about* the ping patch it themselves.
    """
    with patch("cold_outreach.core.llm.verify_llm_credentials", return_value=None):
        yield


@pytest.fixture(autouse=True)
def never_open_a_real_mailbox():
    """Every pass measures the pool's warm capacity, and measuring means an IMAP login.

    Same rule as the provider ping above: reaching a real mailbox from a test suite is
    a bug wherever it happens, and the tests that are *about* the measurement drive it
    themselves (`emails/test_warmth.py` patches the Sent-folder read).
    """
    with patch("cold_outreach.emails.warmth.measure_pool", return_value=None):
        yield


@pytest.fixture
def operator(db):
    """The one active staff user the sender runs as."""
    return UserFactory()


@pytest.fixture
def site_config(db, operator, monkeypatch):
    """The environment a message can be written from, and the config that reads it."""
    monkeypatch.setenv(MESSAGE_ENV["product_docs"], PRODUCT_DOCS)
    monkeypatch.setenv(MESSAGE_ENV["campaign_target"], CAMPAIGN_TARGET)
    return SiteConfig.load()
