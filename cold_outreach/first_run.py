"""What a first run has to collect before a message can be written or sent.

Four things, and none of them is a preference. A campaign has to say what is being
sold and to whom, or the agent has nothing to write from. A model has to be reachable,
or there is nothing to write it with. The mail has to be signed by somebody, and
self-hosted means that somebody is the one operator this install runs as. And it has to
leave a mailbox.

**Two of the four are credentials, and both are checked before they are stored** — the
mailbox by its SMTP login, the model by one ping. Neither provider has a health API,
and an accepted login is the only gate there is. The alternative is what the LLM key
used to do: sit in a variable nobody read until an agent asked for a model, halfway
through a pass, with a lead already chosen.

**`outsend send` calls this before any mail moves**, so an operator who wired the pipe
into a timer never discovers a setup step they did not know to run.

**The environment first, the terminal second — and only if there is one.** This runs
from a timer as often as from a shell, where nothing can be prompted, so a headless run
with something missing raises one error naming every variable that would have satisfied
it. An interactive wizard blocking a timer is the one outcome to avoid.

**Everything it asks is asked on stderr**, the caret included, because stdout belongs to
the pipe this program sits on the end of.
"""
from __future__ import annotations

import getpass
import logging
import os
import sys

from cold_outreach.core.models import LLM_ENV
from cold_outreach.errors import OutsendError
from cold_outreach.leads.campaigns import CONFIG_ENV, hydrate_from_environment, missing_config

logger = logging.getLogger(__name__)

# Who the mail comes from. The name is required — the agent writes as a person and the
# sign-off is that person. The address is not: blind-copying yourself is a convenience,
# and an operator who declines one is not misconfigured.
OPERATOR_ENV = {"name": "OUTSEND_OPERATOR_NAME", "email": "OUTSEND_OPERATOR_EMAIL"}

# The box itself. Both are required, and the password is the provider's app password —
# a Google box rejects its login password outright.
MAILBOX_ENV = {"address": "OUTSEND_MAILBOX_ADDRESS", "password": "OUTSEND_MAILBOX_PASSWORD"}

# Where the box lives. Defaulted to Google Workspace by the model, because that is what
# most connected boxes are; every other provider names its four values here. They are
# not prompted for: four transport questions at onboarding is the wizard this is trying
# not to be, and a wrong host arrives as rejected credentials, which the error says.
TRANSPORT_ENV = {
    "host": "OUTSEND_SMTP_HOST",
    "port": "OUTSEND_SMTP_PORT",
    "imap_host": "OUTSEND_IMAP_HOST",
    "imap_port": "OUTSEND_IMAP_PORT",
}

# The sign-off appended to every send from the box. Optional, and never a reason to
# stop: a message with no sign-off still goes out.
SIGNATURE_ENV = "OUTSEND_SIGNATURE"

_PROMPTS = {
    "product_docs": "What do you sell? Paste anything that explains it — a page, notes, a pitch.",
    "campaign_target": "Who is this campaign for?",
    "booking_link": "A link to book a call, if you have one (blank to skip).",
    "ai_model": "Which model writes the mail? A provider:model id, "
                "e.g. anthropic:claude-sonnet-4-5-20250929.",
    "llm_api_key": "Its API key.",
    "name": "Who is sending this mail? The name that signs it.",
    "email": "Your own address, to blind-copy every send to (blank to skip).",
    "address": "The address to send from.",
    "password": "Its app password — not the password you log in with.",
    "signature": "How do you sign off? (blank for no sign-off).",
}


def ensure_ready(campaign) -> None:
    """Collect everything a send needs, or stop naming the variables that would.

    One error at the end rather than one per round trip: a timer that is missing three
    things should learn all three from a single failure mail.
    """
    interactive = sys.stdin.isatty()
    missing = [
        *_ensure_campaign(campaign, interactive),
        *_ensure_llm(interactive),
        *_ensure_operator(interactive),
        *_ensure_mailbox(interactive),
    ]
    if missing:
        raise OutsendError(f"not ready to send — set {', '.join(missing)}")


# ── The campaign ──────────────────────────────────────────────────


def _ensure_campaign(campaign, interactive: bool) -> list[str]:
    """Fill the campaign's empty fields. Returns the variables still unanswered."""
    hydrate_from_environment(campaign)
    missing = missing_config(campaign)
    if missing and interactive:
        _prompt_campaign(campaign, missing)
        missing = missing_config(campaign)
    return [CONFIG_ENV[field] for field in missing]


def _prompt_campaign(campaign, missing: list[str]) -> None:
    """Ask for the fields that are still empty, and save what the operator answers.

    `booking_link` rides along with them: it is never required, but an operator being
    asked about this campaign anyway is the only moment it is cheap to ask.
    """
    for field in [*missing, "booking_link"]:
        if getattr(campaign, field):
            continue
        answer = _ask(_PROMPTS[field])
        if answer:
            setattr(campaign, field, answer)
    campaign.save()


# ── The model ─────────────────────────────────────────────────────


def _ensure_llm(interactive: bool) -> list[str]:
    """Store the model and key this install writes with. Returns the variables still unanswered.

    Skipped once both are stored, because verifying them costs a round trip to the
    provider and a pass that already has credentials has nothing to check — the same
    bargain the mailbox strikes with its SMTP login.
    """
    from cold_outreach.core.llm import verify_llm_credentials
    from cold_outreach.core.models import SiteConfig, hydrate_llm_from_environment, missing_llm_config

    config = SiteConfig.load()
    if not missing_llm_config(config):
        return []

    hydrate_llm_from_environment(config)
    missing = missing_llm_config(config)
    if missing and interactive:
        _prompt_llm(config, missing)
        missing = missing_llm_config(config)
    if missing:
        return [LLM_ENV[field] for field in missing]

    refused = verify_llm_credentials(config.ai_model, config.llm_api_key, config.llm_api_base)
    if refused:
        raise OutsendError(f"{config.ai_model} refused these credentials: {refused}")

    config.save()
    logger.info("writing with %s", config.ai_model)
    return []


def _prompt_llm(config, missing: list[str]) -> None:
    """Ask for whichever of the two is still empty, without saving either.

    Nothing is written here: `_ensure_llm` stores the row only after the model has
    answered, so a mistyped key leaves no half-configured install to clear out before
    the next attempt.
    """
    if "ai_model" in missing:
        config.ai_model = _ask(_PROMPTS["ai_model"])
    if "llm_api_key" in missing:
        config.llm_api_key = _ask_secret(_PROMPTS["llm_api_key"])


# ── The operator ──────────────────────────────────────────────────


def _ensure_operator(interactive: bool) -> list[str]:
    """Record who this install sends as. Returns the variables still unanswered.

    Skipped entirely once an operator exists: this is identity, not configuration, and
    re-asking a named operator on every pass would be a prompt that never ends.
    """
    from cold_outreach.core.operator import get_active_user, set_operator

    if get_active_user() is not None:
        return []

    name = _from_environment(OPERATOR_ENV["name"])
    email = _from_environment(OPERATOR_ENV["email"])
    if not name and interactive:
        name = _ask(_PROMPTS["name"])
        email = email or _ask(_PROMPTS["email"])
    if not name:
        return [OPERATOR_ENV["name"]]

    set_operator(full_name=name, email=email)
    logger.info("sending as %s", name)
    return []


# ── The mailbox ───────────────────────────────────────────────────


def _ensure_mailbox(interactive: bool) -> list[str]:
    """Connect a mailbox. Returns the variables still unanswered.

    Skipped once any box exists, because storing one costs an SMTP round trip and a
    pass that already has somewhere to send from has nothing to check.
    """
    from cold_outreach.emails.models import Mailbox

    if Mailbox.objects.exists():
        return []

    address = _from_environment(MAILBOX_ENV["address"])
    password = _from_environment(MAILBOX_ENV["password"])
    if interactive:
        address = address or _ask(_PROMPTS["address"])
        password = password or _ask_secret(_PROMPTS["password"])
    given = {"address": address, "password": password}
    if not all(given.values()):
        return [variable for key, variable in MAILBOX_ENV.items() if not given[key]]

    box, reason = Mailbox.objects.create_verified(
        from_address=address, password=password, **_transport())
    if box is None:
        raise OutsendError(
            f"{address} was not connected: {reason} — a box that is not on Google "
            f"Workspace needs {', '.join(TRANSPORT_ENV.values())}")

    logger.info("connected %s", box.from_address)
    _sign_off(box, interactive)
    return []


def _transport() -> dict[str, str | int]:
    """Host and port for both protocols, from the environment or the model's defaults.

    The defaults are read off the fields themselves rather than restated here: two
    copies of `smtp.gmail.com` is one place for them to disagree.
    """
    from cold_outreach.emails.models import Mailbox

    values: dict[str, str | int] = {}
    for field, variable in TRANSPORT_ENV.items():
        default = Mailbox._meta.get_field(field).default
        given = _from_environment(variable)
        if not given:
            values[field] = default
        elif isinstance(default, int):
            if not given.isdigit():
                raise OutsendError(f"{variable} must be a port number, not {given!r}")
            values[field] = int(given)
        else:
            values[field] = given
    return values


def _sign_off(box, interactive: bool) -> None:
    """Ask, once, how this box signs its mail.

    NULL and "" are different answers: NULL means nobody has been asked, "" means the
    operator declined one. Collapsing them would ask a declining operator again on
    every run, which is why the column is nullable in the first place.
    """
    if box.signature is not None:
        return

    signature = os.environ.get(SIGNATURE_ENV)
    if signature is None and interactive:
        signature = _ask(_PROMPTS["signature"])
    if signature is None:
        return

    box.signature = signature.strip()
    box.save(update_fields=["signature"])


# ── Asking ────────────────────────────────────────────────────────


def _from_environment(variable: str) -> str:
    """One `OUTSEND_*` value, stripped — "" whether it was unset or blank."""
    return (os.environ.get(variable) or "").strip()


def _ask(question: str) -> str:
    """Ask on stderr and read one line back.

    The caret goes to stderr too: `input`'s own prompt argument writes to stdout, which
    on this program is the pipe.
    """
    print(f"\n{question}", file=sys.stderr)
    print("> ", end="", file=sys.stderr, flush=True)
    return input().strip()


def _ask_secret(question: str) -> str:
    """Ask for something that must not echo — an app password in a shared terminal."""
    print(f"\n{question}", file=sys.stderr)
    return getpass.getpass("> ", stream=sys.stderr).strip()
