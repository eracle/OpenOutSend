# cold_outreach/core/models.py
"""What this install writes with, kept where the install keeps everything else.

What the message is written from, the operator and the mailbox all live in the store
and are *seeded* from `OUTSEND_*` on a first run. The model and its key were the one
exception — read from the environment at the point of use — and the exception was the
defect: an install whose timer unit had lost the variable got no answer at startup, it
got a traceback in the middle of a pass, per lead, after a mailbox was already open and
a lead was already chosen. Everything else a send needs is checked before any mail
moves; this now is too.

**The environment seeds an empty row and never overwrites a filled one.** A value the
operator has edited is the answer, and re-reading a stale variable over it would be a
silent revert — the same rule the message fields already follow.

**Nothing is stored until the provider has answered to it**, the way a mailbox is
stored only once its SMTP login succeeds. There is no other gate: a key is either one
the provider accepts or one that will fail on a lead.

This does not make the store a secret it was not already — `Mailbox` has held an app
password since the transport arrived, so `~/.openoutsend/data/db.sqlite3` was always a
file to keep.
"""
from __future__ import annotations

import os

from django.db import models

# What a first run reads each field from. The environment is the only way in a timer has.
LLM_ENV = {
    "ai_model": "OUTSEND_AI_MODEL",
    "llm_api_key": "OUTSEND_LLM_API_KEY",
    "llm_api_base": "OUTSEND_LLM_API_BASE",
}

# `llm_api_base` is not among them: only the `openai_compatible` provider reads it, and
# that builder raises its own error naming it. Requiring it here would stop an Anthropic
# install over a value it never looks at.
REQUIRED_LLM_FIELDS = ("ai_model", "llm_api_key")

# The three things `outsend init` collects beyond the model — what a message is
# written from. Blank rather than null: "not asked yet" and "deliberately empty" are
# the same state to a prompt template, and the template renders every one of them as
# text.
MESSAGE_ENV = {
    "product_docs": "OUTSEND_PRODUCT_DOCS",
    "campaign_target": "OUTSEND_CAMPAIGN_TARGET",
    "booking_link": "OUTSEND_BOOKING_LINK",
}

# `booking_link` is not among them: the prompt renders its whole block only when there
# is one, and an install that never offers a call is a normal install.
REQUIRED_MESSAGE_FIELDS = ("product_docs", "campaign_target")


class SiteConfig(models.Model):
    """The one row this install runs on.

    A singleton rather than a settings module because these are *stored* answers: the
    operator can change the model without touching a unit file, and a run that starts
    can say what it will write with rather than discovering it mid-send.
    """

    # A pydantic-ai model identifier in `provider:model` form. The provider lives inside
    # this single string — there is no separate provider field to drift out of sync. A
    # bare name whose prefix is unambiguous (`gpt`/`o1`/`o3`→openai, `claude`→anthropic,
    # `gemini`→google) is also accepted; see core/llm.py:split_model_id.
    ai_model = models.CharField(
        max_length=200, blank=True, default="",
        help_text="provider:model, e.g. anthropic:claude-sonnet-4-5-20250929",
    )
    llm_api_key = models.CharField(max_length=500, blank=True, default="")
    # Only consulted for the openai_compatible provider (OpenRouter / Together / Ollama / vLLM).
    llm_api_base = models.CharField(max_length=500, blank=True, default="")

    # What this install sells, and to whom — the two things the outreach agent
    # cannot write a message without.
    product_docs = models.TextField(blank=True, default="")
    campaign_target = models.TextField(blank=True, default="")
    # Never required: the prompt renders its whole booking block only when this is set.
    booking_link = models.CharField(max_length=500, blank=True, default="")

    class Meta:
        verbose_name = "Site Configuration"
        verbose_name_plural = "Site Configuration"

    def __str__(self):
        return "Site Configuration"

    def save(self, *args, **kwargs):
        """Pin the row. One install, one configuration — a second row is a second answer."""
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> "SiteConfig":
        """The configuration, created empty the first time it is asked for."""
        config, _ = cls.objects.get_or_create(pk=1)
        return config


def hydrate_llm_from_environment(config: SiteConfig) -> list[str]:
    """Fill this config's empty fields from `OUTSEND_*`. Returns what it wrote.

    **Assigns, and does not save.** The caller stores the row only once the provider has
    accepted the credentials, so a wrong key set in a unit file leaves nothing behind to
    clear out by hand before the right one can be tried.
    """
    written = []
    for field, variable in LLM_ENV.items():
        value = (os.environ.get(variable) or "").strip()
        if value and not getattr(config, field):
            setattr(config, field, value)
            written.append(field)
    return written


def missing_llm_config(config: SiteConfig) -> list[str]:
    """The required fields still empty, in the order a first run asks for them."""
    return [field for field in REQUIRED_LLM_FIELDS if not getattr(config, field)]


def hydrate_message_from_environment(config: SiteConfig) -> list[str]:
    """Fill this config's empty message fields from `OUTSEND_*`. Returns what it wrote.

    Empty fields only: the environment seeds a config, it does not overwrite one an
    operator has already edited. Silent and never fatal, because ingest runs behind a
    timer and a message is not being written yet — what is missing surfaces at the
    first send, where it matters.
    """
    written = []
    for field, variable in MESSAGE_ENV.items():
        value = (os.environ.get(variable) or "").strip()
        if value and not getattr(config, field):
            setattr(config, field, value)
            written.append(field)
    if written:
        config.save(update_fields=written)
    return written


def missing_message_config(config: SiteConfig) -> list[str]:
    """The fields a message cannot be written without, and that this config lacks."""
    return [field for field in REQUIRED_MESSAGE_FIELDS if not getattr(config, field)]
