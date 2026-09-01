"""What a run has to be given before a message can be written or sent.

Four things, and none of them is a preference. This install has to say what is being
sold and to whom, or the agent has nothing to write from. A model has to be reachable,
or there is nothing to write it with. The mail has to be signed by somebody, and
self-hosted means that somebody is the one operator this install runs as. And it has to
leave a mailbox.

**Everything comes from the environment. Nothing is asked.** This program runs from a
timer as often as from a shell, and it is the right-hand side of a pipe besides — both
are places where nothing can be prompted. So a run that is missing something raises one
error naming every variable that would have satisfied it, rather than blocking on a
question nobody is there to answer. An operator who wants to be asked runs the wizard in
OpenOutreach, which owns the human half and exports these names.

**Two of the four are credentials, and both are checked before anything is stored** —
the mailbox by its SMTP login, the model by one ping. Neither provider has a health API,
and an accepted login is the only gate there is. This is what makes reading config fresh
on every run safe: the failure an operator used to hit halfway through a pass, with a
mailbox open and a lead already chosen, is caught here instead.

**`outsend send` calls this before any mail moves**, so an operator who wired the pipe
into a timer never discovers a setup step they did not know to run.

**What is stored is what was measured**, and only that: the mailbox row holds its own
spacing clock and the capacity it learned from the provider, seeded once from the
environment because a box is connected by an SMTP login rather than by a variable.
"""
from __future__ import annotations

import logging
import os

from cold_outreach.core.config import LLM_ENV, MESSAGE_ENV
from cold_outreach.errors import OutsendError

logger = logging.getLogger(__name__)

# Who the mail comes from. The name is required — the agent writes as a person and the
# sign-off is that person. The address is not: blind-copying yourself is a convenience,
# and an operator who declines one is not misconfigured.
OPERATOR_ENV = {"name": "OUTSEND_OPERATOR_NAME", "email": "OUTSEND_OPERATOR_EMAIL"}

# The box itself. Both are required, and the password is the provider's app password —
# a Google box rejects its login password outright.
MAILBOX_ENV = {"address": "OUTSEND_MAILBOX_ADDRESS", "password": "OUTSEND_MAILBOX_PASSWORD"}

# Where the box lives. Defaulted to Google Workspace by the model, because that is what
# most connected boxes are; every other provider names its four values here. A wrong host
# arrives as rejected credentials, which the error says.
TRANSPORT_ENV = {
    "host": "OUTSEND_SMTP_HOST",
    "port": "OUTSEND_SMTP_PORT",
    "imap_host": "OUTSEND_IMAP_HOST",
    "imap_port": "OUTSEND_IMAP_PORT",
}

# The sign-off appended to every send from the box. Optional, and never a reason to
# stop: a message with no sign-off still goes out.
SIGNATURE_ENV = "OUTSEND_SIGNATURE"


def check_ready() -> None:
    """Verify this run has everything a send needs, or stop naming what would give it.

    One error at the end rather than one per round trip: a timer that is missing three
    things should learn all three from a single failure mail.
    """
    missing = [
        *_check_message(),
        *_check_llm(),
        *_ensure_operator(),
        *_ensure_mailbox(),
    ]
    if missing:
        raise OutsendError(f"not ready to send — set {', '.join(missing)}")


# ── What a message is written from ──────────────────────────────────


def _check_message() -> list[str]:
    """The message fields this run was not given."""
    from cold_outreach.core.config import SiteConfig, missing_message_config

    return [MESSAGE_ENV[field] for field in missing_message_config(SiteConfig.load())]


# ── The model ─────────────────────────────────────────────────────


def _check_llm() -> list[str]:
    """Confirm the model will answer to the key it was given.

    The ping costs a round trip and runs on every pass, which is the price of not
    storing the answer. It buys the thing storing never did: a key rotated out from
    under a running timer fails here, before a lead is chosen, rather than mid-pass.
    """
    from cold_outreach.core.config import SiteConfig, missing_llm_config
    from cold_outreach.core.llm import verify_llm_credentials

    config = SiteConfig.load()
    missing = missing_llm_config(config)
    if missing:
        return [LLM_ENV[field] for field in missing]

    refused = verify_llm_credentials(config.ai_model, config.llm_api_key, config.llm_api_base)
    if refused:
        raise OutsendError(f"{config.ai_model} refused these credentials: {refused}")

    logger.info("writing with %s", config.ai_model)
    return []


# ── The operator ──────────────────────────────────────────────────


def _ensure_operator() -> list[str]:
    """Record who this install sends as. Returns the variables still unanswered.

    Skipped once an operator exists: this is identity, and the Django `User` row is what
    the rest of the codebase reads. Both children share it under one registry.
    """
    from cold_outreach.core.operator import get_active_user, set_operator

    if get_active_user() is not None:
        return []

    name = _from_environment(OPERATOR_ENV["name"])
    if not name:
        return [OPERATOR_ENV["name"]]

    set_operator(full_name=name, email=_from_environment(OPERATOR_ENV["email"]))
    logger.info("sending as %s", name)
    return []


# ── The mailbox ───────────────────────────────────────────────────


def _ensure_mailbox() -> list[str]:
    """Connect a mailbox. Returns the variables still unanswered.

    Skipped once any box exists, because storing one costs an SMTP round trip and a
    pass that already has somewhere to send from has nothing to check. The row is
    seeded from the environment and then belongs to the pipeline: what it accumulates
    afterwards — the spacing clock, the capacity the provider taught it — is measured,
    and no variable can restate it.
    """
    from cold_outreach.emails.models import Mailbox

    if Mailbox.objects.exists():
        return []

    given = {key: _from_environment(variable) for key, variable in MAILBOX_ENV.items()}
    if not all(given.values()):
        return [variable for key, variable in MAILBOX_ENV.items() if not given[key]]

    box, reason = Mailbox.objects.create_verified(
        from_address=given["address"], password=given["password"], **_transport())
    if box is None:
        raise OutsendError(
            f"{given['address']} was not connected: {reason} — a box that is not on "
            f"Google Workspace needs {', '.join(TRANSPORT_ENV.values())}")

    logger.info("connected %s", box.from_address)
    _sign_off(box)
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


def _sign_off(box) -> None:
    """Record how this box signs its mail, if the environment says.

    NULL and "" are different answers: NULL means nothing was given, "" means an empty
    sign-off was asked for on purpose. Collapsing them would make a deliberately blank
    signature indistinguishable from an unset variable.
    """
    signature = os.environ.get(SIGNATURE_ENV)
    if signature is None:
        return

    box.signature = signature.strip()
    box.save(update_fields=["signature"])


def _from_environment(variable: str) -> str:
    """One `OUTSEND_*` value, stripped — "" whether it was unset or blank."""
    return (os.environ.get(variable) or "").strip()
