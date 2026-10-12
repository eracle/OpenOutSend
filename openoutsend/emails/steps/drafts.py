# openoutsend/emails/steps/drafts.py
"""What the agent wrote, checked before it goes: placeholders filled, banned phrases refused.

A step prompt can ask the model to write ``{order_url}`` instead of a link. **The model
copies the placeholder and the engine fills it in**, from the fields the deal was piped
with, so a link, a price or a date is never typed by the model and can never be typed
wrong. Then the message is checked for two facts about the text:

- **a placeholder naming a field the deal does not carry**, which would reach the reader
  as ``{order_url}`` in braces;
- **a banned phrase** (``core/conf.banned_phrases``), case-insensitive, in the message
  as it would be sent.

Either is **redrafted once**, with the failure named, and a second failure is **held**:
never sent, the reason kept on the deal (``Deal.held``), which takes it out of every send
pool until its line is piped again. A hold is not retried pass after pass, because a
missing field fails the same way every time and each try costs two model calls.

This is not a judgement of the writing — that lives in the prompt (*Rules live in the
prompt*, `p2-e2-message-carries-a-face`). A missing field and a banned phrase are facts,
checked the same way every time.

Steps and follow-ups are checked; a reply is not. It answers what the lead wrote, from a
prompt that names no field.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def write_checked(write, prompt: str, fields: dict):
    """Have *write* answer *prompt*, redraft once if the draft fails, and fill it in.

    *write* takes a prompt and returns an ``OutreachDecision``. Returns the decision —
    with its placeholders filled when it is a message — and the failures of the last
    draft, empty when it may go.
    """
    decision = write(prompt)
    failures = _failures(decision, fields)
    if failures:
        logger.info("draft refused (%s) — redrafting once", "; ".join(failures))
        decision = write(f"{prompt}\n\n{_redraft_note(failures)}")
        failures = _failures(decision, fields)
    if failures or decision.action != "send_message":
        return decision, failures
    return decision.model_copy(update={
        "subject": fill(decision.subject, fields) if decision.subject else decision.subject,
        "message": fill(decision.message, fields),
    }), []


def check(subject: str | None, message: str, fields: dict) -> list[str]:
    """What is wrong with *subject* and *message*, as they would be filled and sent."""
    texts = [text for text in (subject, message) if text]
    unknown = sorted({name for text in texts for name in PLACEHOLDER.findall(text)} - fields.keys())
    failures = [f"unknown placeholder {{{name}}}" for name in unknown]
    if failures:
        return failures
    return [f'banned phrase "{phrase}"' for phrase in _banned_in(
        " ".join(fill(text, fields) for text in texts))]


def fill(text: str, fields: dict) -> str:
    """*text* with every ``{field}`` replaced by its value. Unknown names are left as written."""
    def value(match):
        name = match[1]
        return str(fields[name]) if name in fields else match[0]

    return PLACEHOLDER.sub(value, text)


def hold(deal, failures: list[str]):
    """Keep *deal* from sending until its line is piped again, and say why. Sends nothing."""
    deal.held = "; ".join(failures)
    logger.warning("%s held, not sent: %s — fix it and pipe the line again",
                   deal.lead.public_id, deal.held)


def _failures(decision, fields: dict) -> list[str]:
    """``check`` for a message, and nothing for any other decision."""
    if decision.action != "send_message" or not decision.message:
        return []
    return check(decision.subject, decision.message, fields)


def _banned_in(text: str) -> list[str]:
    """Every banned phrase in *text*, case-insensitively."""
    from openoutsend.core.conf import banned_phrases

    lowered = text.lower()
    return [phrase for phrase in banned_phrases() if phrase.lower() in lowered]


def _redraft_note(failures: list[str]) -> str:
    """What the second draft is told about the first."""
    listed = "\n".join(f"- {failure}" for failure in failures)
    return (
        "Your previous draft of this email could not be sent:\n"
        f"{listed}\n\n"
        "Write it again without these. Use only placeholders this prompt names, "
        "and none of the phrases listed."
    )
