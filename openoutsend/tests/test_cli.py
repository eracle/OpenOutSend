"""`outsend`'s surface: what the verbs are, and what a send does before it sends."""
from __future__ import annotations

from argparse import Namespace
from unittest.mock import patch

import pytest

from openoutsend.__main__ import _parse_args, _send
from openoutsend.core.config import LLM_ENV, MESSAGE_ENV
from openoutsend.errors import OutsendError
from openoutsend.send_pass import PassResult
from openoutsend.tests.emails import maillog

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def unconfigured_environment(monkeypatch):
    """No `OUTSEND_*` in scope, so a test about a missing field is about that field."""
    for variable in [*MESSAGE_ENV.values(), *LLM_ENV.values()]:
        monkeypatch.delenv(variable, raising=False)


@pytest.fixture
def connected(site_config, monkeypatch):
    """A message config, a model, an operator and a box — an install ready to send.

    What the readiness check *verifies* is `test_first_run.py`'s subject; what is tested
    here is only that a send refuses to move mail until it has been given.
    """
    from openoutsend.core.config import SiteConfig

    monkeypatch.setenv(LLM_ENV["ai_model"], "anthropic:claude-sonnet-4-5-20250929")
    monkeypatch.setenv(LLM_ENV["llm_api_key"], "sk-ada")
    maillog.mailbox()
    return SiteConfig.load()


def _args(**kwargs) -> Namespace:
    return Namespace(**{"command": "send", "prompt_line": None, "sequence": None,
                        "count": None, "debug": False, "agent_draft": False,
                        "subject": None, "body": None, "json_output": False, **kwargs})


# ── The surface ───────────────────────────────────────────────────


def test_no_verb_is_the_pipe():
    assert _parse_args([]).command is None


@pytest.mark.parametrize("verb", ["check", "send"])
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


@pytest.mark.parametrize("word", ["all", "ALL"])
def test_the_pool_itself_can_be_the_goal(word):
    """`send all` spares the operator looking up a count that goes stale as they type it."""
    from openoutsend.send_job import ALL

    assert _parse_args(["send", word]).count == ALL


def test_a_count_belongs_to_send():
    """`outsend check 5` means nothing, and silently ignoring the 5 is worse than saying so."""
    with pytest.raises(SystemExit):
        _parse_args(["check", "5"])


# ── --sequence ────────────────────────────────────────────────────


@pytest.mark.parametrize("argv", [["--sequence", "s"], ["send", "--sequence", "s"]])
def test_the_pipe_and_send_take_a_sequence(argv):
    assert _parse_args(argv).sequence == "s"


def test_check_takes_no_sequence():
    with pytest.raises(SystemExit):
        _parse_args(["check", "--sequence", "s"])


def test_send_passes_the_sequence_folder_as_its_key(connected, tmp_path):
    (tmp_path / "E0-hello.prompt.j2").write_text("Say hello.")
    with patch("openoutsend.send_pass.run_send_pass", return_value=PassResult()) as run:
        _send(_args(sequence=str(tmp_path)))

    assert run.call_args.kwargs["sequence"] == tmp_path.name


def test_a_bad_sequence_folder_ends_the_command_before_any_mail_moves(connected, tmp_path):
    with patch("openoutsend.send_pass.run_send_pass") as run, \
            pytest.raises(OutsendError, match="no such sequence folder"):
        _send(_args(sequence=str(tmp_path / "nope")))

    run.assert_not_called()


def test_the_pipe_enrols_into_the_named_sequence(db, tmp_path, monkeypatch):
    import io

    from openoutsend.__main__ import _ingest
    from openoutsend.leads.models import Deal

    (tmp_path / "E0-hello.prompt.j2").write_text("Say hello.")
    monkeypatch.setattr("sys.stdin", io.StringIO('{"lead_id": "a", "email": "a@b.com"}\n'))

    assert _ingest(_args(command=None, sequence=str(tmp_path))) == 0
    assert Deal.objects.get().sequence == tmp_path.name


# ── --agent-draft ─────────────────────────────────────────────────


def test_agent_draft_parses():
    assert _parse_args(["send", "--agent-draft"]).agent_draft is True


def test_agent_draft_with_a_count_is_refused():
    with pytest.raises(SystemExit):
        _parse_args(["send", "5", "--agent-draft"])


def test_subject_and_body_need_agent_draft():
    with pytest.raises(SystemExit):
        _parse_args(["send", "--subject", "Hi", "--body", "there"])


def test_a_body_alone_answers_a_later_step():
    assert _parse_args(["send", "--agent-draft", "--body", "More."]).body == "More."


def test_subject_without_a_body_is_refused():
    with pytest.raises(SystemExit):
        _parse_args(["send", "--agent-draft", "--subject", "Hi"])


def test_draft_pending_renders_as_one_typed_line(connected, capsys):
    from openoutsend.errors import DraftPending
    from openoutsend.__main__ import main

    with patch("openoutsend.__main__._boot"), \
            patch("openoutsend.send_pass.run_send_pass",
                 side_effect=DraftPending("needs an opener", payload={"company": "Acme"})):
        code = main(["send", "--agent-draft"])

    assert code == 1
    assert capsys.readouterr().err.strip() == "error: draft_pending: needs an opener"


def test_draft_pending_carries_its_payload_under_json(connected, capsys):
    import json

    from openoutsend.errors import DraftPending
    from openoutsend.__main__ import main

    with patch("openoutsend.__main__._boot"), \
            patch("openoutsend.send_pass.run_send_pass",
                 side_effect=DraftPending("needs an opener", payload={"company": "Acme"})):
        code = main(["send", "--agent-draft", "--json"])

    assert code == 1
    document = json.loads(capsys.readouterr().err)
    assert document == {"error": {"type": "draft_pending", "message": "needs an opener",
                                  "company": "Acme"}}


# ── Sending ───────────────────────────────────────────────────────


def test_send_runs_one_pass(connected, capsys):
    with patch("openoutsend.send_pass.run_send_pass",
               return_value=PassResult(mirrored=2, answered=1, opened=3)) as run:
        assert _send(_args()) == 0

    from openoutsend.core.agent_draft import OFF
    run.assert_called_once_with(None, agent_draft=OFF, sequence=None)
    narration = capsys.readouterr().err
    assert ("read 2 new message(s) · answered 1 · sent 0 later step(s) · followed up 0 · "
            "opened 3") in narration


def test_a_count_runs_to_the_goal_instead_of_passing_once(connected, capsys):
    """`send 5` hands the goal to the run; the pass is what the run calls, not the CLI."""
    from openoutsend.send_job import SendJobResult

    run = SendJobResult(goal=5, passes=6, totals=PassResult(opened=5, answered=2))
    with patch("openoutsend.send_job.run_send_job", return_value=run) as job, \
            patch("openoutsend.send_pass.run_send_pass") as single:
        assert _send(_args(count=5)) == 0

    job.assert_called_once_with(5, None, sequence=None)
    single.assert_not_called()
    assert "opened 5 of 5 conversation(s) in 6 pass(es)" in capsys.readouterr().err


def test_a_goal_the_run_could_not_reach_is_said_and_carried_into_the_exit_code(
        connected, capsys):
    """*3 of 5* and *5 of 5* are the same counts; only this line tells them apart."""
    from openoutsend.send_job import DRAINED, SendJobResult

    run = SendJobResult(goal=5, totals=PassResult(opened=3), stopped_because=DRAINED,
                        detail="3 of 5 — nobody left to email")
    with patch("openoutsend.send_job.run_send_job", return_value=run):
        assert _send(_args(count=5)) == 1

    assert "stopped at 3 of 5 — nobody left to email" in capsys.readouterr().err


def test_draining_the_pool_is_how_send_all_succeeds(connected, capsys):
    """The ending that fails `send 5` is the ending `send all` was asking for."""
    from openoutsend.send_job import ALL, DRAINED, SendJobResult

    run = SendJobResult(goal=ALL, passes=9, totals=PassResult(opened=26),
                        stopped_because=DRAINED, detail="26 opened — nobody left to email")
    with patch("openoutsend.send_job.run_send_job", return_value=run):
        assert _send(_args(count=ALL)) == 0

    assert "opened 26 conversation(s) in 9 pass(es) — nobody left to email" in \
        capsys.readouterr().err


def test_a_failed_send_is_reported_and_carried_into_the_exit_code(connected, capsys):
    with patch("openoutsend.send_pass.run_send_pass", return_value=PassResult(failed=1)):
        assert _send(_args()) == 1

    assert "1 send(s) failed" in capsys.readouterr().err


def test_a_send_stops_on_an_install_nothing_configured(db, capsys):
    """`check` runs implicitly, and what it produces is an error — never a prompt."""
    with patch("openoutsend.send_pass.run_send_pass") as run, \
            pytest.raises(OutsendError, match="OUTSEND_PRODUCT_DOCS"):
        _send(_args())

    run.assert_not_called()


def test_a_send_with_no_mailbox_never_reaches_the_pass(site_config, capsys):
    """The one thing a pass cannot work around: nowhere to send from."""
    with patch("openoutsend.send_pass.run_send_pass") as run, \
            pytest.raises(OutsendError, match="OUTSEND_MAILBOX_ADDRESS"):
        _send(_args())

    run.assert_not_called()


def test_a_send_with_no_model_never_reaches_the_pass(site_config):
    """The key used to be missed until an agent asked for a model, mid-pass, per lead."""
    maillog.mailbox()

    with patch("openoutsend.send_pass.run_send_pass") as run, \
            pytest.raises(OutsendError, match=LLM_ENV["llm_api_key"]):
        _send(_args())

    run.assert_not_called()


def test_agent_draft_never_needs_an_llm_key(site_config):
    """The whole point of --agent-draft is a calling agent writing the opener instead
    of a second, separately-keyed model — an install running only that way is not
    asked for a key it will never spend."""
    maillog.mailbox()

    with patch("openoutsend.send_pass.run_send_pass") as run:
        _send(_args(agent_draft=True))

    run.assert_called_once()


def test_a_terminal_is_not_asked_either(db, capsys):
    """A TTY changes nothing: this program is the right of a pipe, and it never prompts."""
    with patch("openoutsend.send_pass.run_send_pass") as run, \
            patch("sys.stdin.isatty", return_value=True), \
            patch("builtins.input", side_effect=AssertionError("asked a question")), \
            pytest.raises(OutsendError, match=MESSAGE_ENV["product_docs"]):
        _send(_args())

    run.assert_not_called()
