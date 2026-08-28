"""`outsend`'s surface: what the verbs are, and what a send does before it sends."""
from __future__ import annotations

from argparse import Namespace
from unittest.mock import patch

import pytest

from cold_outreach.__main__ import _parse_args, _send
from cold_outreach.errors import OutsendError
from cold_outreach.leads.campaigns import CONFIG_ENV
from cold_outreach.send_pass import PassResult
from cold_outreach.tests.emails import maillog
from cold_outreach.tests.factories import CampaignFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def unconfigured_environment(monkeypatch):
    """No `OUTSEND_*` in scope, so a test about a missing field is about that field."""
    for variable in CONFIG_ENV.values():
        monkeypatch.delenv(variable, raising=False)


@pytest.fixture
def connected(campaign):
    """A campaign, an operator and a box to send from — an install past its first run.

    What that first run *collects* is `test_first_run.py`'s subject; what is tested here
    is only that a send refuses to move mail until it has been collected.
    """
    maillog.mailbox()
    return campaign


def _args(**kwargs) -> Namespace:
    return Namespace(**{"command": "send", "campaign": None, "prompt_line": None,
                        "count": None, "debug": False, **kwargs})


# ── The surface ───────────────────────────────────────────────────


def test_no_verb_is_the_pipe():
    assert _parse_args([]).command is None


@pytest.mark.parametrize("verb", ["init", "send"])
def test_the_two_verbs_that_are_not_on_the_pipe(verb):
    assert _parse_args([verb]).command == verb


def test_an_unknown_verb_is_refused():
    with pytest.raises(SystemExit):
        _parse_args(["export"])


def test_a_bare_send_has_no_count_and_is_one_pass():
    """What the timer fires, and what it has always fired."""
    assert _parse_args(["send"]).count is None


def test_send_takes_a_goal_as_its_object():
    assert _parse_args(["send", "5"]).count == 5


@pytest.mark.parametrize("count", ["0", "-1", "some"])
def test_a_goal_that_is_not_a_number_of_conversations_is_refused(count):
    with pytest.raises(SystemExit):
        _parse_args(["send", count])


def test_a_count_belongs_to_send():
    """`outsend init 5` means nothing, and silently ignoring the 5 is worse than saying so."""
    with pytest.raises(SystemExit):
        _parse_args(["init", "5"])


# ── Sending ───────────────────────────────────────────────────────


def test_send_runs_one_pass_over_the_resolved_campaign(connected, capsys):
    with patch("cold_outreach.send_pass.run_send_pass",
               return_value=PassResult(mirrored=2, answered=1, opened=3)) as run:
        assert _send(_args()) == 0

    run.assert_called_once_with(connected, None)
    narration = capsys.readouterr().err
    assert f"campaign: {connected.name}" in narration
    assert "read 2 new message(s) · answered 1 · followed up 0 · opened 3" in narration


def test_a_count_runs_to_the_goal_instead_of_passing_once(connected, capsys):
    """`send 5` hands the goal to the run; the pass is what the run calls, not the CLI."""
    from cold_outreach.send_job import SendJobResult

    run = SendJobResult(goal=5, passes=6, totals=PassResult(opened=5, answered=2))
    with patch("cold_outreach.send_job.run_send_job", return_value=run) as job, \
            patch("cold_outreach.send_pass.run_send_pass") as single:
        assert _send(_args(count=5)) == 0

    job.assert_called_once_with(connected, 5, None)
    single.assert_not_called()
    assert "opened 5 of 5 conversation(s) in 6 pass(es)" in capsys.readouterr().err


def test_a_goal_the_run_could_not_reach_is_said_and_carried_into_the_exit_code(
        connected, capsys):
    """*3 of 5* and *5 of 5* are the same counts; only this line tells them apart."""
    from cold_outreach.send_job import DRAINED, SendJobResult

    run = SendJobResult(goal=5, totals=PassResult(opened=3), stopped_because=DRAINED,
                        detail="3 of 5 — nobody left to email")
    with patch("cold_outreach.send_job.run_send_job", return_value=run):
        assert _send(_args(count=5)) == 1

    assert "stopped at 3 of 5 — nobody left to email" in capsys.readouterr().err


def test_a_failed_send_is_reported_and_carried_into_the_exit_code(connected, capsys):
    with patch("cold_outreach.send_pass.run_send_pass", return_value=PassResult(failed=1)):
        assert _send(_args()) == 1

    assert "1 send(s) failed" in capsys.readouterr().err


def test_a_headless_send_stops_on_an_install_nothing_configured(db, capsys):
    """`init` runs implicitly — and from a timer that means an error, never a prompt."""
    CampaignFactory(product_docs="", campaign_target="")

    with patch("cold_outreach.send_pass.run_send_pass") as run, \
            patch("sys.stdin.isatty", return_value=False), \
            pytest.raises(OutsendError, match="OUTSEND_PRODUCT_DOCS"):
        _send(_args())

    run.assert_not_called()


def test_a_send_with_no_mailbox_never_reaches_the_pass(campaign, capsys):
    """The one thing a pass cannot work around: nowhere to send from."""
    with patch("cold_outreach.send_pass.run_send_pass") as run, \
            patch("sys.stdin.isatty", return_value=False), \
            pytest.raises(OutsendError, match="OUTSEND_MAILBOX_ADDRESS"):
        _send(_args())

    run.assert_not_called()


def test_an_interactive_send_asks_for_what_is_missing(connected):
    connected.product_docs, connected.campaign_target = "", ""
    connected.save()

    with patch("cold_outreach.send_pass.run_send_pass", return_value=PassResult()), \
            patch("sys.stdin.isatty", return_value=True), \
            patch("builtins.input", side_effect=["A lead finder", "Founders", ""]):
        assert _send(_args()) == 0

    connected.refresh_from_db()
    assert (connected.product_docs, connected.campaign_target) == ("A lead finder", "Founders")
