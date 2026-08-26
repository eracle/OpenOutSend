"""`outsend` — the command on the right of the pipe.

    openoutreach find 50 --json | outsend

**It takes no verb.** A program that is itself the send step does not need a `send`
subcommand, and things on the right of a pipe conventionally take none: `| less`,
`| jq`, `| tee`. The one argument it has is the campaign, and that one is usually
absent too. `init` is the single exception, because it is not on the pipe at all.

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
outsend init [--campaign NAME]   collect what a campaign needs to write with"""


def main(argv: list[str] | None = None) -> int:
    """Entry point for the `outsend` console script. Returns the exit code."""
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    _configure_logging(args.debug)
    _boot()

    from cold_outreach.errors import OutsendError

    try:
        return _init(args) if args.command == "init" else _ingest(args)
    except OutsendError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="outsend", usage=USAGE)
    parser.add_argument("command", nargs="?", choices=["init"])
    parser.add_argument("--campaign", default=None,
                        help="which campaign these leads belong to; required only if there are several")
    parser.add_argument("--debug", action="store_true", help="log what each step decided")
    return parser.parse_args(argv)


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


def _ingest(args: argparse.Namespace) -> int:
    """Read stdin into the resolved campaign and report what happened."""
    from cold_outreach.leads.campaigns import hydrate_from_environment, resolve_campaign
    from cold_outreach.leads.ingest import ingest

    campaign = resolve_campaign(args.campaign)
    hydrate_from_environment(campaign)
    print(f"campaign: {campaign.name}", file=sys.stderr)

    result = ingest(sys.stdin, campaign)
    print(f"stored {result.stored} lead(s)", file=sys.stderr)
    if result.suppressed:
        print(f"{result.suppressed} of them are suppressed and will not be emailed", file=sys.stderr)
    if result.skipped:
        print(f"{result.skipped} line(s) skipped — see above; the rest are stored", file=sys.stderr)
    return 0 if result.ok else 1


# ── First run ─────────────────────────────────────────────────────


def _init(args: argparse.Namespace) -> int:
    """Collect what a campaign needs before a message means anything.

    The environment first, then the terminal — and **only** if there is one. The send
    pass may be running from a timer, where nothing can be prompted, so a headless run
    with something missing stops with an error naming the variables that would have
    satisfied it. An interactive wizard blocking a timer is the one outcome to avoid.
    """
    from cold_outreach.errors import OutsendError
    from cold_outreach.leads.campaigns import CONFIG_ENV, hydrate_from_environment, missing_config, resolve_campaign

    campaign = resolve_campaign(args.campaign)
    hydrate_from_environment(campaign)
    print(f"campaign: {campaign.name}", file=sys.stderr)

    missing = missing_config(campaign)
    if missing and sys.stdin.isatty():
        _prompt_for(campaign, missing)
        missing = missing_config(campaign)
    if missing:
        variables = ", ".join(CONFIG_ENV[field] for field in missing)
        raise OutsendError(f"campaign {campaign.name} is missing {', '.join(missing)} — set {variables}")

    print(f"campaign {campaign.name} is ready to write with", file=sys.stderr)
    return 0


_PROMPTS = {
    "product_docs": "What do you sell? Paste anything that explains it — a page, notes, a pitch.",
    "campaign_target": "Who is this campaign for?",
    "booking_link": "A link to book a call, if you have one (blank to skip).",
}


def _prompt_for(campaign, missing: list[str]) -> None:
    """Ask the operator for the fields that are still empty, and save what they answer.

    Written to stderr like every other narration, so a `2>` capture holds the whole
    conversation and stdout is still the pipe's.
    """
    for field in [*missing, "booking_link"]:
        if getattr(campaign, field):
            continue
        print(f"\n{_PROMPTS[field]}", file=sys.stderr)
        answer = input("> ").strip()
        if answer:
            setattr(campaign, field, answer)
    campaign.save()


if __name__ == "__main__":
    raise SystemExit(main())
