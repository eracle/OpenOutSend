"""Facts — what we know about a person, before anybody has written to them.

**`Lead.profile_summary`** is extracted from the raw firmographic `profile_text` the
pipe carries: `{"facts": [...]}`, short self-contained sentences a prompt can render as
bullets. Built lazily on the first touch and cached.

What a *conversation* teaches is not summarised at all: every message is written after
the whole history with the lead, verbatim (`core/agents/history.py`).

**The extraction is tuned for the reader it has**, which is the one thing the finder's
version got wrong: it conditioned on campaign target and product docs and asked for
*durable facts over fleeting commentary* — right for a verdict, backwards for an
opener, which wants the specific and the recent. That is why the profile extraction
here takes no campaign context at all and asks for what distinguishes this person.

**mem0's four-event reconciliation protocol is not ported.** ADD/UPDATE/DELETE/NONE
existed because facts lived in a vector store addressed by id. Here the store is a
flat JSON list on one row, so returning the merged list *is* the operation, and a
protocol on top of it would be an id space with nothing to key.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Enough to describe a person without turning the prompt into the transcript it
# summarises. The finder used the same ceiling.
MAX_FACTS = 30

_PROFILE_PROMPT = """\
You are an information-extraction assistant. Read the text and produce a flat list
of atomic, self-contained facts about the person it describes.

Rules:
- Each fact is one complete sentence that stands on its own, under ~25 words.
- Prefer what is **specific and distinguishing** — this company, this role, this
  market, this stack, where they are, what they appear to be building or hiring for.
  A fact true of ten thousand people ("works in technology") is not worth a line.
- Never invent. If the text does not assert it, it is not a fact.
- Merge near-duplicates. Return between 0 and {max_facts} facts; an empty list is a
  fine answer when the text says nothing useful.
"""


class FactList(BaseModel):
    """Structured LLM output for the extraction."""

    facts: list[str] = Field(
        default_factory=list,
        description="Atomic, self-contained factual statements about the lead.",
    )


def facts_of(summary: dict | None) -> list[str]:
    """The fact list inside a summary blob, or `[]`. The one place its shape is read."""
    return list((summary or {}).get("facts") or [])


# ── The LLM boundary ──────────────────────────────────────────────


def _run_extraction(system: str, text: str) -> list[str]:
    """One structured call. Returns the facts, capped."""
    from pydantic_ai import Agent

    from openoutsend.core.llm import get_llm_model, run_agent_sync

    agent = Agent(
        get_llm_model(),
        system_prompt=system,
        output_type=FactList,
        # No temperature: the current Claude models reject one that isn't their default.
        model_settings={"timeout": 60},
    )
    result: FactList = run_agent_sync(agent.run(text)).output
    return [fact for fact in result.facts if fact.strip()][:MAX_FACTS]


# ── The lead's profile ────────────────────────────────────────────


def materialize_profile_summary_if_missing(lead) -> None:
    """Build `lead.profile_summary` on first touch. No-op once it is built.

    Lazy, and that is where the cost belongs: the extraction is paid for only for
    people actually written to, where the finder would have paid it for every lead
    it ever qualified — including everyone nobody mails.
    """
    if facts_of(lead.profile_summary):
        return
    if not lead.profile_text:
        logger.warning("profile_summary: %s has no profile_text to extract from", lead.public_id)
        return

    facts = _run_extraction(_PROFILE_PROMPT.format(max_facts=MAX_FACTS), lead.profile_text)
    lead.profile_summary = {"facts": facts}
    lead.save(update_fields=["profile_summary"])
    logger.info(
        "profile facts for %s (%d)%s",
        lead.public_id, len(facts), "".join(f"\n  • {fact}" for fact in facts),
    )
