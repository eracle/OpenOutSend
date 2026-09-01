"""What a run has to be given: the message fields, the model, the operator, a mailbox.

The rule under all of it is one rule: **the environment, and nothing else**. A run short
of something stops naming every variable that would have answered it, and never asks —
not headless, and not on a terminal either, because this program is the right-hand side
of a pipe and a question there has nobody to answer it.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from cold_outreach.core.config import LLM_ENV, MESSAGE_ENV, SiteConfig
from cold_outreach.core.operator import get_active_user, seller_full_name
from cold_outreach.emails.models import Mailbox
from cold_outreach.errors import OutsendError
from cold_outreach.first_run import (
    MAILBOX_ENV,
    OPERATOR_ENV,
    SIGNATURE_ENV,
    TRANSPORT_ENV,
    check_ready,
)
from cold_outreach.tests.emails import maillog
from cold_outreach.tests.factories import UserFactory

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


@pytest.fixture
def configured(monkeypatch):
    """Everything a send needs, in the environment — the ordinary case."""
    for variable, value in _EVERYTHING.items():
        monkeypatch.setenv(variable, value)


@pytest.fixture(autouse=True)
def llm():
    """The provider's ping, answered without a network."""
    with patch("cold_outreach.core.llm.verify_llm_credentials", return_value=None) as ping:
        yield ping


@pytest.fixture
def smtp():
    """The auth gate, answered without a network."""
    with patch("cold_outreach.emails.smtp.verify_auth", return_value=(True, "")) as auth:
        yield auth


# ── What a run is given ───────────────────────────────────────────


def test_a_run_names_everything_it_is_missing_at_once():
    """One error, not one per round trip: a timer's failure mail is the only report."""
    with pytest.raises(OutsendError) as stopped:
        check_ready()

    message = str(stopped.value)
    for variable in [MESSAGE_ENV["product_docs"], MESSAGE_ENV["campaign_target"],
                     LLM_ENV["ai_model"], LLM_ENV["llm_api_key"],
                     OPERATOR_ENV["name"], *MAILBOX_ENV.values()]:
        assert variable in message


def test_the_environment_is_enough_to_be_ready(configured, smtp):
    check_ready()

    config = SiteConfig.load()
    assert config.product_docs and config.campaign_target
    assert seller_full_name() == "Ada Lovelace"
    assert get_active_user().email == "ada@corp.com"
    assert config.llm_api_key == "sk-ada"
    assert Mailbox.objects.get().from_address == "ada@corp.com"
    smtp.assert_called_once_with("smtp.gmail.com", 587, "ada@corp.com", "app-pw")


def test_nothing_a_human_answered_is_written_to_the_store(configured, smtp):
    """The config is read, used and dropped — there is no row for it to land in."""
    from django.apps import apps

    check_ready()

    assert list(apps.get_app_config("outsend_core").get_models()) == []


def test_a_second_pass_reconnects_nothing(configured, smtp):
    """A box already stored costs no SMTP login: connecting is a first-run expense."""
    check_ready()
    smtp.reset_mock()

    check_ready()

    smtp.assert_not_called()


# ── The model ─────────────────────────────────────────────────────


def test_the_model_is_read_fresh_on_every_run(configured, smtp, monkeypatch):
    """The environment is the answer, so changing it changes the next run — no revert."""
    check_ready()
    assert SiteConfig.load().ai_model == "anthropic:claude-sonnet-4-5-20250929"

    monkeypatch.setenv(LLM_ENV["ai_model"], "openai:gpt-4o")
    check_ready()

    assert SiteConfig.load().ai_model == "openai:gpt-4o"


def test_the_key_is_pinged_on_every_run(configured, smtp, llm):
    """The price of not storing it: a key rotated under a timer fails here, not mid-pass."""
    check_ready()
    check_ready()

    assert llm.call_count == 2


def test_a_missing_key_stops_the_run_before_any_mail_moves(configured, smtp, llm, monkeypatch):
    """The failure this step exists for: named up front, not raised mid-pass per lead."""
    monkeypatch.delenv(LLM_ENV["llm_api_key"])

    with pytest.raises(OutsendError, match=LLM_ENV["llm_api_key"]):
        check_ready()

    llm.assert_not_called()


def test_a_key_the_provider_refuses_says_why(configured, smtp):
    """An accepted credential is the only one worth going on with."""
    with patch("cold_outreach.core.llm.verify_llm_credentials",
               return_value="invalid x-api-key"), \
            pytest.raises(OutsendError, match="invalid x-api-key"):
        check_ready()

    assert not Mailbox.objects.exists()


# ── The operator ──────────────────────────────────────────────────


def test_the_operator_is_recorded_once_and_never_re_read(configured, smtp, monkeypatch):
    """Identity is a row, not a variable: the `User` both children read is written once."""
    check_ready()

    monkeypatch.setenv(OPERATOR_ENV["name"], "Grace Hopper")
    check_ready()

    assert seller_full_name() == "Ada Lovelace"


def test_an_operator_who_declines_a_bcc_address_is_still_complete(configured, smtp, monkeypatch):
    monkeypatch.delenv(OPERATOR_ENV["email"])

    check_ready()

    assert get_active_user().email == ""
    assert seller_full_name() == "Ada Lovelace"


def test_a_run_with_no_operator_name_names_the_variable(configured, smtp, monkeypatch):
    monkeypatch.delenv(OPERATOR_ENV["name"])

    with pytest.raises(OutsendError, match=OPERATOR_ENV["name"]):
        check_ready()


# ── The mailbox ───────────────────────────────────────────────────


def test_rejected_credentials_store_nothing_and_say_why(configured):
    with patch("cold_outreach.emails.smtp.verify_auth",
               return_value=(False, "auth rejected (535)")), \
            pytest.raises(OutsendError, match="auth rejected"):
        check_ready()

    assert not Mailbox.objects.exists()


def test_a_box_that_is_not_on_google_names_its_own_transport(configured, smtp, monkeypatch):
    monkeypatch.setenv(TRANSPORT_ENV["host"], "smtp.fastmail.com")
    monkeypatch.setenv(TRANSPORT_ENV["port"], "465")
    monkeypatch.setenv(TRANSPORT_ENV["imap_host"], "imap.fastmail.com")
    monkeypatch.setenv(TRANSPORT_ENV["imap_port"], "993")

    check_ready()

    box = Mailbox.objects.get()
    assert (box.host, box.port, box.imap_host, box.imap_port) == (
        "smtp.fastmail.com", 465, "imap.fastmail.com", 993)


def test_a_port_that_is_not_a_number_stops_before_the_connection(configured, smtp, monkeypatch):
    monkeypatch.setenv(TRANSPORT_ENV["port"], "five-eight-seven")

    with pytest.raises(OutsendError, match=TRANSPORT_ENV["port"]):
        check_ready()

    smtp.assert_not_called()


def test_a_stored_box_keeps_what_it_measured(configured, smtp):
    """The row is the pipeline's: its clock and its learned ceiling outlive any variable."""
    UserFactory()
    box = maillog.mailbox()
    box.daily_limit = 17
    box.save(update_fields=["daily_limit"])

    check_ready()

    box.refresh_from_db()
    assert box.daily_limit == 17
    smtp.assert_not_called()


# ── The sign-off ──────────────────────────────────────────────────


def test_a_sign_off_from_the_environment_lands_on_the_box(configured, smtp, monkeypatch):
    monkeypatch.setenv(SIGNATURE_ENV, "Ada\nCorp")

    check_ready()

    assert Mailbox.objects.get().signature == "Ada\nCorp"


def test_an_empty_sign_off_is_an_answer_and_not_an_absence(configured, smtp, monkeypatch):
    """NULL means nothing was given, "" means an empty sign-off was asked for."""
    monkeypatch.setenv(SIGNATURE_ENV, "")

    check_ready()

    assert Mailbox.objects.get().signature == ""


def test_a_run_with_no_sign_off_variable_leaves_it_unset(configured, smtp):
    check_ready()

    assert Mailbox.objects.get().signature is None


# ── Nothing is asked ──────────────────────────────────────────────


def test_a_terminal_is_never_prompted(smtp, monkeypatch):
    """A TTY is not a licence to block: the pipe's right-hand side asks nobody anything."""
    monkeypatch.setenv(MESSAGE_ENV["product_docs"], "A self-hosted lead finder.")

    with patch("sys.stdin.isatty", return_value=True), \
            patch("builtins.input", side_effect=AssertionError("asked a question")), \
            patch("getpass.getpass", side_effect=AssertionError("asked for a secret")), \
            pytest.raises(OutsendError, match=MESSAGE_ENV["campaign_target"]):
        check_ready()


def test_nothing_it_says_reaches_stdout(configured, smtp, capsys):
    """stdout is the pipe's, so what a check narrates goes to stderr or to the log."""
    check_ready()

    assert capsys.readouterr().out == ""
