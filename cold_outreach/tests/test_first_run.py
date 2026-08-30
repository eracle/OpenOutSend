"""What a first run collects: the message fields, the model, the operator, a mailbox.

The rule under all of it is the same one twice: the environment first, the terminal
second and only if there is one — and a headless run that is still short of something
stops with the variables that would have answered it, rather than asking a timer a
question.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from cold_outreach.core.models import LLM_ENV, MESSAGE_ENV, SiteConfig
from cold_outreach.core.operator import get_active_user, seller_full_name
from cold_outreach.emails.models import Mailbox
from cold_outreach.errors import OutsendError
from cold_outreach.first_run import (
    MAILBOX_ENV,
    OPERATOR_ENV,
    SIGNATURE_ENV,
    TRANSPORT_ENV,
    ensure_ready,
)
from cold_outreach.tests.emails import maillog
from cold_outreach.tests.factories import SiteConfigFactory, UserFactory

pytestmark = pytest.mark.django_db

_EVERYTHING = {
    MESSAGE_ENV["product_docs"]: "A self-hosted lead finder.",
    MESSAGE_ENV["campaign_target"]: "Founders at small B2B software companies.",
    LLM_ENV["ai_model"]: "anthropic:claude-sonnet-4-5-20250929",
    LLM_ENV["llm_api_key"]: "sk-ada",
    OPERATOR_ENV["name"]: "Ada Lovelace",
    OPERATOR_ENV["email"]: "ada@corp.com",
    MAILBOX_ENV["address"]: "ada@corp.com",
    MAILBOX_ENV["password"]: "app-pw",
}


@pytest.fixture(autouse=True)
def unconfigured_environment(monkeypatch):
    """No `OUTSEND_*` in scope, so a test about a missing value is about that value."""
    for variable in [*MESSAGE_ENV.values(), *LLM_ENV.values(), *OPERATOR_ENV.values(),
                     *MAILBOX_ENV.values(), *TRANSPORT_ENV.values(), SIGNATURE_ENV]:
        monkeypatch.delenv(variable, raising=False)


@pytest.fixture(autouse=True)
def llm():
    """The provider's ping, answered without a network.

    Autouse because storing credentials is now part of nearly every path through
    `ensure_ready`; the tests that are *about* the ping patch it themselves.
    """
    with patch("cold_outreach.core.llm.verify_llm_credentials", return_value=None) as ping:
        yield ping


@pytest.fixture
def stored_llm(db):
    """An install whose model is already configured, so that step asks nothing."""
    return SiteConfig.objects.create(
        ai_model="anthropic:claude-sonnet-4-5-20250929", llm_api_key="sk-ada")


@pytest.fixture
def headless():
    """No terminal to ask, which is what a timer looks like."""
    with patch("sys.stdin.isatty", return_value=False):
        yield


@pytest.fixture
def terminal():
    """A terminal, so the prompts are allowed to run."""
    with patch("sys.stdin.isatty", return_value=True):
        yield


@pytest.fixture
def smtp():
    """The auth gate, answered without a network."""
    with patch("cold_outreach.emails.smtp.verify_auth", return_value=(True, "")) as auth:
        yield auth


# ── Headless ──────────────────────────────────────────────────────


def test_a_headless_run_names_everything_it_is_missing_at_once(headless):
    """One error, not one per round trip: a timer's failure mail is the only report."""
    with pytest.raises(OutsendError) as stopped:
        ensure_ready()

    message = str(stopped.value)
    for variable in [MESSAGE_ENV["product_docs"], MESSAGE_ENV["campaign_target"],
                     LLM_ENV["ai_model"], LLM_ENV["llm_api_key"],
                     OPERATOR_ENV["name"], *MAILBOX_ENV.values()]:
        assert variable in message


def test_the_environment_is_enough_to_be_ready(headless, smtp, monkeypatch):
    for variable, value in _EVERYTHING.items():
        monkeypatch.setenv(variable, value)

    ensure_ready()

    config = SiteConfig.load()
    assert config.product_docs and config.campaign_target
    assert seller_full_name() == "Ada Lovelace"
    assert get_active_user().email == "ada@corp.com"
    assert config.llm_api_key == "sk-ada"
    assert Mailbox.objects.get().from_address == "ada@corp.com"
    smtp.assert_called_once_with("smtp.gmail.com", 587, "ada@corp.com", "app-pw")


def test_nothing_is_asked_of_a_timer_that_already_has_it_all(headless, smtp, llm, stored_llm):
    """An install that is set up costs a send pass nothing — no SMTP login, no ping."""
    UserFactory()
    maillog.mailbox()
    SiteConfigFactory()

    ensure_ready()

    smtp.assert_not_called()
    llm.assert_not_called()


# ── The model ─────────────────────────────────────────────────────


def test_the_model_and_its_key_are_stored_not_re_read(headless, smtp, monkeypatch):
    """The environment seeds the row once; the run then works from the row."""
    for variable, value in _EVERYTHING.items():
        monkeypatch.setenv(variable, value)

    ensure_ready()

    config = SiteConfig.load()
    assert (config.ai_model, config.llm_api_key) == (
        "anthropic:claude-sonnet-4-5-20250929", "sk-ada")


def test_a_variable_never_overwrites_what_the_operator_edited(headless, smtp, stored_llm,
                                                              monkeypatch):
    """A stale unit file must not silently revert a model changed in the store."""
    for variable, value in _EVERYTHING.items():
        monkeypatch.setenv(variable, value)
    SiteConfig.objects.filter(pk=stored_llm.pk).update(ai_model="openai:gpt-4o")
    UserFactory()
    maillog.mailbox()

    ensure_ready()

    assert SiteConfig.load().ai_model == "openai:gpt-4o"


def test_a_missing_key_stops_the_run_before_any_mail_moves(headless, smtp, monkeypatch):
    """The failure this step exists for: named up front, not raised mid-pass per lead."""
    for variable, value in _EVERYTHING.items():
        monkeypatch.setenv(variable, value)
    monkeypatch.delenv(LLM_ENV["llm_api_key"])

    with pytest.raises(OutsendError, match=LLM_ENV["llm_api_key"]):
        ensure_ready()

    assert not SiteConfig.objects.exclude(ai_model="").exists()


def test_a_key_the_provider_refuses_stores_nothing_and_says_why(headless, smtp, monkeypatch):
    """Same bargain as the mailbox: an accepted credential is the only one worth keeping."""
    for variable, value in _EVERYTHING.items():
        monkeypatch.setenv(variable, value)

    with patch("cold_outreach.core.llm.verify_llm_credentials",
               return_value="invalid x-api-key"), \
            pytest.raises(OutsendError, match="invalid x-api-key"):
        ensure_ready()

    assert SiteConfig.load().llm_api_key == ""


def test_the_key_is_asked_for_without_echoing_it(terminal, smtp, monkeypatch):
    monkeypatch.setenv(SIGNATURE_ENV, "")
    for variable in [MAILBOX_ENV["address"], OPERATOR_ENV["name"]]:
        monkeypatch.setenv(variable, "ada@corp.com")
    monkeypatch.setenv(MAILBOX_ENV["password"], "app-pw")
    UserFactory()
    SiteConfigFactory()

    with patch("builtins.input", return_value="anthropic:claude-sonnet-4-5-20250929"), \
            patch("getpass.getpass", return_value="sk-ada") as secret:
        ensure_ready()

    secret.assert_called_once()
    assert SiteConfig.load().ai_model == "anthropic:claude-sonnet-4-5-20250929"


# ── The operator ──────────────────────────────────────────────────


def test_the_operator_is_asked_once_and_never_again(terminal, smtp, stored_llm, monkeypatch):
    monkeypatch.setenv(MAILBOX_ENV["address"], "ada@corp.com")
    monkeypatch.setenv(MAILBOX_ENV["password"], "app-pw")
    monkeypatch.setenv(SIGNATURE_ENV, "")
    SiteConfigFactory()

    with patch("builtins.input", side_effect=["Ada Lovelace", "ada@corp.com"]):
        ensure_ready()

    # A second pass asks nothing: identity is not re-collected, and the box is connected.
    with patch("builtins.input", side_effect=AssertionError("asked again")):
        ensure_ready()

    assert get_active_user().first_name == "Ada"


def test_an_operator_who_declines_a_bcc_address_is_still_complete(
        terminal, smtp, stored_llm, monkeypatch):
    monkeypatch.setenv(MAILBOX_ENV["address"], "ada@corp.com")
    monkeypatch.setenv(MAILBOX_ENV["password"], "app-pw")
    monkeypatch.setenv(SIGNATURE_ENV, "")
    SiteConfigFactory()

    with patch("builtins.input", side_effect=["Ada", ""]):
        ensure_ready()

    assert get_active_user().email == ""
    assert seller_full_name() == "Ada"


# ── The mailbox ───────────────────────────────────────────────────


def test_rejected_credentials_store_nothing_and_say_why(headless, monkeypatch):
    for variable, value in _EVERYTHING.items():
        monkeypatch.setenv(variable, value)

    with patch("cold_outreach.emails.smtp.verify_auth",
               return_value=(False, "auth rejected (535)")), \
            pytest.raises(OutsendError, match="auth rejected"):
        ensure_ready()

    assert not Mailbox.objects.exists()


def test_a_box_that_is_not_on_google_names_its_own_transport(headless, smtp, monkeypatch):
    for variable, value in _EVERYTHING.items():
        monkeypatch.setenv(variable, value)
    monkeypatch.setenv(TRANSPORT_ENV["host"], "smtp.fastmail.com")
    monkeypatch.setenv(TRANSPORT_ENV["port"], "465")
    monkeypatch.setenv(TRANSPORT_ENV["imap_host"], "imap.fastmail.com")
    monkeypatch.setenv(TRANSPORT_ENV["imap_port"], "993")

    ensure_ready()

    box = Mailbox.objects.get()
    assert (box.host, box.port, box.imap_host, box.imap_port) == (
        "smtp.fastmail.com", 465, "imap.fastmail.com", 993)


def test_a_port_that_is_not_a_number_stops_before_the_connection(headless, smtp, monkeypatch):
    for variable, value in _EVERYTHING.items():
        monkeypatch.setenv(variable, value)
    monkeypatch.setenv(TRANSPORT_ENV["port"], "five-eight-seven")

    with pytest.raises(OutsendError, match=TRANSPORT_ENV["port"]):
        ensure_ready()

    smtp.assert_not_called()


def test_the_password_is_asked_for_without_echoing_it(terminal, smtp, stored_llm, monkeypatch):
    monkeypatch.setenv(SIGNATURE_ENV, "")
    UserFactory()
    SiteConfigFactory()

    with patch("builtins.input", return_value="ada@corp.com"), \
            patch("getpass.getpass", return_value="app-pw") as secret:
        ensure_ready()

    secret.assert_called_once()
    assert Mailbox.objects.get().from_address == "ada@corp.com"


# ── The sign-off ──────────────────────────────────────────────────


def test_a_declined_sign_off_sticks_and_is_not_asked_again(terminal, smtp, stored_llm):
    """NULL is never asked, "" is declined — collapsing them re-asks forever."""
    UserFactory()
    SiteConfigFactory()

    with patch("builtins.input", side_effect=["ada@corp.com", ""]), \
            patch("getpass.getpass", return_value="app-pw"):
        ensure_ready()

    assert Mailbox.objects.get().signature == ""


def test_a_sign_off_from_the_environment_lands_on_the_box(headless, smtp, monkeypatch):
    for variable, value in _EVERYTHING.items():
        monkeypatch.setenv(variable, value)
    monkeypatch.setenv(SIGNATURE_ENV, "Ada\nCorp")

    ensure_ready()

    assert Mailbox.objects.get().signature == "Ada\nCorp"


def test_a_headless_run_leaves_an_unasked_sign_off_unasked(headless, smtp, monkeypatch):
    for variable, value in _EVERYTHING.items():
        monkeypatch.setenv(variable, value)

    ensure_ready()

    assert Mailbox.objects.get().signature is None


# ── What a message is written from ──────────────────────────────────


def test_the_message_fields_are_asked_for_on_a_terminal(terminal, smtp, stored_llm, monkeypatch):
    monkeypatch.setenv(MAILBOX_ENV["address"], "ada@corp.com")
    monkeypatch.setenv(MAILBOX_ENV["password"], "app-pw")
    monkeypatch.setenv(SIGNATURE_ENV, "")
    UserFactory()

    with patch("builtins.input", side_effect=["A lead finder", "Founders", "cal.com/ada"]):
        ensure_ready()

    config = SiteConfig.load()
    assert (config.product_docs, config.campaign_target, config.booking_link) == (
        "A lead finder", "Founders", "cal.com/ada")


def test_nothing_it_asks_reaches_stdout(terminal, smtp, stored_llm, capsys, monkeypatch):
    """stdout is the pipe's, so the questions and their carets both go to stderr."""
    monkeypatch.setenv(SIGNATURE_ENV, "")
    SiteConfigFactory()

    with patch("builtins.input", side_effect=["Ada", "", "ada@corp.com"]), \
            patch("getpass.getpass", return_value="app-pw"):
        ensure_ready()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Who is sending this mail?" in captured.err
