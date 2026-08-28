"""`outsend` — the command on the right of the pipe.

    openoutreach find 50 --json | outsend

**On the pipe it takes no verb.** Things on the right of a pipe conventionally take
none: `| less`, `| jq`, `| tee`. The one argument it has is the campaign, and that one
is usually absent too. The two verbs it does have are the ones that are not on the pipe
at all: `send`, which reads no stdin and mails what is already stored, and `init`,
which asks for what a first run needs — what the campaign sells and to whom, who is
signing the mail, and the mailbox it leaves from.

**Ingesting and sending are separate invocations on purpose.** A pipe's right-hand side
must not block on the network while a producer is still writing, and the cadence the two
want is different — leads arrive when `find` runs, mail moves on the mailbox's clock. So
the cron line is two entries, not one command doing both.

**stdout is reserved and stays clean**, so the stream composes and a receipt can
never corrupt it. Everything narrated — the campaign resolved, the counts, a skipped
line, Django's own migration chatter — goes to stderr.

**The exit code is the only acknowledgement a pipe can carry**: 0 means the rows are
durably persisted. Non-zero after a skipped line does *not* mean *nothing to
reconcile* — the good rows are stored and a re-run is safe, because ingest is
idempotent; it means a producer emitted something unreadable and somebody has to know.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

USAGE = """outsend [--campaign NAME]        read JSON Lines on stdin, store them, exit
outsend send [N|all] [--campaign NAME] [--prompt-line ID]
                                 one pass: read the mail, answer replies, open what the
                                 guards allow right now. With N, keep at it until N
                                 conversations are open, waiting out the send clocks;
                                 with `all`, until nobody is left to email
outsend init [--campaign NAME]   collect what a first run needs — the campaign, who you
                                 are, and a mailbox to send from"""


def main(argv: list[str] | None = None) -> int:
    """Entry point for the `outsend` console script. Returns the exit code."""
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    _configure_logging(args.debug)
    _boot()

    from cold_outreach.errors import OutsendError

    try:
        return {"init": _init, "send": _send}.get(args.command, _ingest)(args)
    except OutsendError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="outsend", usage=USAGE)
    parser.add_argument("command", nargs="?", choices=["init", "send"])
    # `send 5` is a goal, `send` is a pass — see `_send`. Positional and optional
    # because the count *is* the verb's object, the way `find 5`'s is.
    parser.add_argument("count", nargs="?", type=_goal, default=None,
                        help="how many conversations to open before returning, or `all` "
                             "to empty the pool; omit for a single pass")
    parser.add_argument("--campaign", default=None,
                        help="which campaign these leads belong to; required only if there are several")
    parser.add_argument("--prompt-line", default=None, dest="prompt_line",
                        help="open every email in this pass with one named prompt line; "
                             "omit to draw one at random per send")
    parser.add_argument("--debug", action="store_true", help="log what each step decided")
    args = parser.parse_args(argv)
    if args.count is not None and args.command != "send":
        parser.error("a count belongs to `send` — `outsend send 5`")
    return args


def _goal(value: str) -> int | str:
    """A number of conversations, or ``all``.

    Zero and minus one are not answers, and neither is a word that is not `all` — a goal
    that cannot be read is worth an error rather than a default nobody asked for.
    """
    from cold_outreach.send_job import ALL

    if value.lower() == ALL:
        return ALL
    if not value.isdigit() or int(value) < 1:
        raise argparse.ArgumentTypeError(
            f"expected a number of conversations or `all`, got {value!r}")
    return int(value)


def _configure_logging(debug: bool) -> None:
    """Everything to stderr, because stdout belongs to the pipe."""
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if debug else logging.INFO,
        format="%(message)s",
    )


def _boot() -> None:
    """Start Django and bring the store up to date.

    Migrations run on every invocation and are a no-op once applied, so a fresh
    install is an ingest that works rather than a traceback about a missing table.
    Their narration is pointed at stderr for the same reason everything else is; a
    migration that *fails* still raises, because that is a broken install and not an
    answer.
    """
    import django

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cold_outreach.settings")
    django.setup()

    from django.core.management import call_command

    call_command("migrate", verbosity=0, stdout=sys.stderr)


# ── The pipe ──────────────────────────────────────────────────────


def _campaign_for(args: argparse.Namespace):
    """The campaign this invocation acts on, hydrated from the environment and narrated.

    Every command starts here, so `--campaign` means the same thing to all of them and
    the operator reads which one was chosen before anything else is printed.
    """
    from cold_outreach.leads.campaigns import hydrate_from_environment, resolve_campaign

    campaign = resolve_campaign(args.campaign)
    hydrate_from_environment(campaign)
    print(f"campaign: {campaign.name}", file=sys.stderr)
    return campaign


def _ingest(args: argparse.Namespace) -> int:
    """Read stdin into the resolved campaign and report what happened."""
    from cold_outreach.leads.ingest import ingest

    result = ingest(sys.stdin, _campaign_for(args))
    print(f"stored {result.stored} lead(s)", file=sys.stderr)
    if result.suppressed:
        print(f"{result.suppressed} of them are suppressed and will not be emailed", file=sys.stderr)
    if result.skipped:
        print(f"{result.skipped} line(s) skipped — see above; the rest are stored", file=sys.stderr)
    return 0 if result.ok else 1


# ── The send pass ─────────────────────────────────────────────────


def _send(args: argparse.Namespace) -> int:
    """Mail what is stored — one pass, or as many as a goal of N conversations takes.

    **The count is the difference between a pass and a run.** `outsend send` does what
    the guards allow this instant and exits, which is what a timer wants and what the
    cron line has always fired. `outsend send 5` is the same thing a goal at a time: it
    keeps passing until five conversations are open, sleeping out the spacing clock, the
    daily ceiling and the sending window in between — the clocks the state machine
    already keeps, read as timestamps rather than polled from outside.

    **`init` runs implicitly here**, because a send is the first moment the campaign's
    fields, the operator's name and a mailbox actually have to be there — and an operator
    who wired the pipe into a timer should not discover a setup step they never ran. On a
    TTY that is the same prompts; headless it is the same error naming the variables,
    raised before any mail moves.

    Exit code: non-zero when something failed on its way out, and — for a goal — when
    the run stopped short of it, so a timer's failure mail carries both. Falling short
    is a real answer rather than a crash: the run says which wall it hit.
    """
    from cold_outreach.first_run import ensure_ready

    campaign = _campaign_for(args)
    ensure_ready(campaign)
    return _run_to_goal(campaign, args) if args.count else _run_one_pass(campaign, args)


def _run_one_pass(campaign, args: argparse.Namespace) -> int:
    from cold_outreach.send_pass import run_send_pass

    result = run_send_pass(campaign, args.prompt_line)
    _report(result)
    return 0 if result.ok else 1


def _run_to_goal(campaign, args: argparse.Namespace) -> int:
    """`outsend send N` (or `all`) — and the one line that says how the run ended.

    A goal that was not reached is reported as its own sentence rather than folded into
    the counts, because *3 of 5* and *5 of 5* are the same four counts and different
    answers to "can I stop running this by hand now".

    **`all` ends on the same empty pool that fails a numbered goal, and says so as a
    success**, because emptying the pool is what it asked for.
    """
    from cold_outreach.send_job import run_send_job

    run = run_send_job(campaign, args.count, args.prompt_line)
    _report(run.totals)
    if run.reached and run.drained_the_pool:
        print(f"opened {run.opened} conversation(s) in {run.passes} pass(es) — "
              "nobody left to email", file=sys.stderr)
    elif run.reached:
        print(f"opened {run.opened} of {run.goal} conversation(s) in {run.passes} pass(es)",
              file=sys.stderr)
    else:
        print(f"stopped at {run.detail}", file=sys.stderr)
    return 0 if run.ok else 1


def _report(result) -> None:
    """The counts a pass or a whole run produced, in the same words for both."""
    print(f"read {result.mirrored} new message(s) · answered {result.answered} · "
          f"followed up {result.followed_up} · opened {result.opened}", file=sys.stderr)
    if result.gave_up:
        print(f"{result.gave_up} lead(s) never answered and were closed", file=sys.stderr)
    if result.failed:
        print(f"{result.failed} send(s) failed — see above; the next pass tries them again",
              file=sys.stderr)


# ── First run ─────────────────────────────────────────────────────


def _init(args: argparse.Namespace) -> int:
    """Collect the three things a send needs, and say what it ended up with.

    The collecting itself lives in `first_run.py`, because `send` does exactly the same
    thing before any mail moves and the two must not drift.
    """
    from cold_outreach.core.operator import seller_full_name
    from cold_outreach.emails.models import Mailbox
    from cold_outreach.first_run import ensure_ready

    campaign = _campaign_for(args)
    ensure_ready(campaign)
    print(f"campaign {campaign.name} is ready: signed by {seller_full_name()}, "
          f"sending from {Mailbox.objects.first()}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
