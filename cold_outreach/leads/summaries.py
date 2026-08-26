"""Facts — what we know about a person, and what a conversation has taught us.

Two fact lists, one LLM boundary, and the same shape on both: `{"facts": [...]}`,
short self-contained sentences a prompt can render as bullets.

- **`Lead.profile_summary`** is extracted from the raw firmographic `profile_text`
  the pipe carries. Built lazily on the first touch, cached, and rebuilt if the text
  is corrected by a later ingest.
- **`Deal.chat_summary`** is rolled forward as replies arrive, so a long thread does
  not depend on the agent's short verbatim window to remember what was said early.

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

_CHAT_PROMPT = """\
You are maintaining a running list of facts about a lead, learned from an email
conversation with them.

You are given the facts known so far and the new messages since. Return the **complete
updated list** — not just the new entries.

Rules:
- Keep every fact that still holds; drop or rewrite any the new messages contradict.
- Add what the new messages teach: their situation, tools, constraints, timing,
  who else is involved, what they said in their own words.
- Each fact is one self-contained sentence under ~25 words. Merge near-duplicates.
- Facts about the **lead only**. Messages tagged [Me] are ours and are context for
  reading theirs, never a source of facts.
- At most {max_facts} facts. When it is full, keep what most changes how we would
  write the next message.
"""


def _identity_binding(seller_name: str) -> str:
    """Bind the [Me] tag to a name, so the lead is not credited with our own.

    Without it a lead who opens with *"Hola Diego, gracias..."* gets `the lead's name
    is Diego` extracted as a fact about them — the tags alone carry no name.
    """
    return (
        f"\nIdentity binding (read carefully):\n"
        f"- [Me] is named {seller_name}.\n"
        f"- When a [Lead] message mentions `{seller_name}`, that is a reference to "
        f"[Me] — never record it as a fact about the lead.\n"
        f"- Any existing fact describing `{seller_name}` as though they were the "
        f"lead is contamination: drop it."
    )


class FactList(BaseModel):
    """Structured LLM output for both extractions."""

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

    from cold_outreach.core.llm import get_llm_model, run_agent_sync

    agent = Agent(
        get_llm_model(),
        system_prompt=system,
        output_type=FactList,
        model_settings={"temperature": 0.0, "timeout": 60},
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


# ── The conversation ──────────────────────────────────────────────


def update_chat_summary(deal, new_messages, *, seller_name: str) -> None:
    """Fold *new_messages* into `deal.chat_summary` and save it.

    The whole list comes back from the model rather than a diff, because the store is
    the list: there is nothing to address a diff against, and a merge that can rewrite
    a stale fact is worth more here than one that can only append.
    """
    if not new_messages:
        return

    known = facts_of(deal.chat_summary)
    system = _CHAT_PROMPT.format(max_facts=MAX_FACTS) + _identity_binding(seller_name)
    facts = _run_extraction(system, _transcript(known, new_messages))

    deal.chat_summary = {"facts": facts}
    deal.save(update_fields=["chat_summary"])
    logger.info(
        "chat facts for %s (%d)%s",
        deal.lead.public_id, len(facts), "".join(f"\n  • {fact}" for fact in facts),
    )


def _transcript(known: list[str], messages) -> str:
    """The prompt's input: what is known, then the new turns, tagged by side."""
    known_block = "\n".join(f"- {fact}" for fact in known) or "(nothing yet)"
    turns = "\n".join(
        f"[{'Me' if message.is_outbound else 'Lead'}]: {(message.body_text or '').strip()}"
        for message in messages
        if (message.body_text or "").strip()
    )
    return f"Facts known so far:\n{known_block}\n\nNew messages:\n{turns or '(none)'}"
