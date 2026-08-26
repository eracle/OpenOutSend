"""`outsend`'s surface: what the verbs are, and what a send does before it sends."""
from __future__ import annotations

from argparse import Namespace
from unittest.mock import patch

import pytest

from cold_outreach.__main__ import _parse_args, _send
from cold_outreach.errors import OutsendError
from cold_outreach.leads.campaigns import CONFIG_ENV
from cold_outreach.send_pass import PassResult
from cold_outreach.tests.factories import CampaignFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def unconfigured_environment(monkeypatch):
    """No `OUTSEND_*` in scope, so a test about a missing field is about that field."""
    for variable in CONFIG_ENV.values():
        monkeypatch.delenv(variable, raising=False)


def _args(**kwargs) -> Namespace:
    return Namespace(**{"command": "send", "campaign": None, "debug": False, **kwargs})


# ── The surface ───────────────────────────────────────────────────


def test_no_verb_is_the_pipe():
    assert _parse_args([]).command is None


@pytest.mark.parametrize("verb", ["init", "send"])
def test_the_two_verbs_that_are_not_on_the_pipe(verb):
    assert _parse_args([verb]).command == verb


def test_an_unknown_verb_is_refused():
    with pytest.raises(SystemExit):
        _parse_args(["export"])


# ── Sending ───────────────────────────────────────────────────────


def test_send_runs_one_pass_over_the_resolved_campaign(campaign, capsys):
    with patch("cold_outreach.send_pass.run_send_pass",
               return_value=PassResult(mirrored=2, answered=1, opened=3)) as run:
        assert _send(_args()) == 0

    run.assert_called_once_with(campaign)
    narration = capsys.readouterr().err
    assert f"campaign: {campaign.name}" in narration
    assert "read 2 new message(s) · answered 1 · opened 3" in narration


def test_a_failed_send_is_reported_and_carried_into_the_exit_code(campaign, capsys):
    with patch("cold_outreach.send_pass.run_send_pass", return_value=PassResult(failed=1)):
        assert _send(_args()) == 1

    assert "1 send(s) failed" in capsys.readouterr().err


def test_a_headless_send_stops_on_a_campaign_nothing_configured(db, capsys):
    """`init` runs implicitly — and from a timer that means an error, never a prompt."""
    CampaignFactory(product_docs="", campaign_target="")

    with patch("cold_outreach.send_pass.run_send_pass") as run, \
            patch("sys.stdin.isatty", return_value=False), \
            pytest.raises(OutsendError, match="OUTSEND_PRODUCT_DOCS"):
        _send(_args())

    run.assert_not_called()


def test_an_interactive_send_asks_for_what_is_missing(db):
    campaign = CampaignFactory(product_docs="", campaign_target="")

    with patch("cold_outreach.send_pass.run_send_pass", return_value=PassResult()), \
            patch("sys.stdin.isatty", return_value=True), \
            patch("builtins.input", side_effect=["A lead finder", "Founders", ""]):
        assert _send(_args()) == 0

    campaign.refresh_from_db()
    assert (campaign.product_docs, campaign.campaign_target) == ("A lead finder", "Founders")
