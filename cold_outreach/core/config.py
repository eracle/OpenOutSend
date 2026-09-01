# cold_outreach/core/config.py
"""What this install writes with, read from the environment on every run.

**These are answers a human gives, and this program is not the one asking.** Remembering
what somebody typed is a convenience for somebody who types; an agent supplies its
environment on every invocation and has nothing to remember. So there is no row and no
singleton here — `OUTSEND_*` is the whole configuration surface, and it is read fresh
each run.

**The defect that put these in the store is fixed by checking, not by storing.** An
install whose timer unit had lost a variable used to get no answer at startup and a
traceback in the middle of a pass — per lead, with a mailbox already open and a lead
already chosen. What prevents that is `first_run.check_ready()` running before any mail
moves and naming every missing variable at once; persistence never had anything to do
with it.

**What the pipeline *measures* still lives in the store** — the mailbox's spacing clock
and its learned capacity, the transport log, suppression. The line is who produced the
value, not whether it is a secret: `Mailbox` has held an app password since the transport
arrived, and still does.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# What each field is read from. The environment is the only way in a timer has, and now
# the only way in at all.
LLM_ENV = {
    "ai_model": "OUTSEND_AI_MODEL",
    "llm_api_key": "OUTSEND_LLM_API_KEY",
    "llm_api_base": "OUTSEND_LLM_API_BASE",
}

# `llm_api_base` is not among them: only the `openai_compatible` provider reads it, and
# that builder raises its own error naming it. Requiring it here would stop an Anthropic
# install over a value it never looks at.
REQUIRED_LLM_FIELDS = ("ai_model", "llm_api_key")

# What a message is written from.
MESSAGE_ENV = {
    "product_docs": "OUTSEND_PRODUCT_DOCS",
    "campaign_target": "OUTSEND_CAMPAIGN_TARGET",
    "booking_link": "OUTSEND_BOOKING_LINK",
}

# `booking_link` is not among them: the prompt renders its whole block only when there
# is one, and an install that never offers a call is a normal install.
REQUIRED_MESSAGE_FIELDS = ("product_docs", "campaign_target")


@dataclass(frozen=True)
class SiteConfig:
    """The configuration this run was given.

    Frozen because nothing may write it: a value edited at runtime would live until the
    process ended and then be gone, which is the silent half-persistence this replaced.
    Empty string rather than None throughout — "unset" and "deliberately blank" are the
    same state to a prompt template, and the template renders every one of them as text.
    """

    # A pydantic-ai model identifier in `provider:model` form. The provider lives inside
    # this single string — there is no separate provider field to drift out of sync. A
    # bare name whose prefix is unambiguous (`gpt`/`o1`/`o3`→openai, `claude`→anthropic,
    # `gemini`→google) is also accepted; see core/llm.py:split_model_id.
    ai_model: str = ""
    llm_api_key: str = ""
    # Only consulted for the openai_compatible provider (OpenRouter / Together / Ollama / vLLM).
    llm_api_base: str = ""

    # What this install sells, and to whom — the two things the outreach agent cannot
    # write a message without.
    product_docs: str = ""
    campaign_target: str = ""
    # Never required: the prompt renders its whole booking block only when this is set.
    booking_link: str = ""

    @classmethod
    def load(cls) -> "SiteConfig":
        """This run's configuration, read from `OUTSEND_*`."""
        return cls(**{field: _from_environment(variable)
                      for field, variable in {**LLM_ENV, **MESSAGE_ENV}.items()})


def missing_llm_config(config: SiteConfig) -> list[str]:
    """The required model fields this run was not given."""
    return [field for field in REQUIRED_LLM_FIELDS if not getattr(config, field)]


def missing_message_config(config: SiteConfig) -> list[str]:
    """The fields a message cannot be written without, and that this run lacks."""
    return [field for field in REQUIRED_MESSAGE_FIELDS if not getattr(config, field)]


def _from_environment(variable: str) -> str:
    """One `OUTSEND_*` value, stripped — "" whether it was unset or blank."""
    return (os.environ.get(variable) or "").strip()
