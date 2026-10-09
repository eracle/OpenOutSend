# openoutsend/core/agents/prompt.py
"""Jinja plumbing + the frame every message is written inside.

The frame (``frame.j2``) is the system prompt around every message: who is writing,
the product, what we know about the lead. It carries no rules of its own — those come
with each prompt (a step file, the follow-up, the reply) — so one frame serves every
kind of message. ``core.agents.outreach`` puts the history and the prompt after it.
"""
from __future__ import annotations

import jinja2

from openoutsend.core.conf import PROMPTS_DIR
from openoutsend.core.config import SiteConfig
from openoutsend.core.operator import seller_full_name
from openoutsend.leads.summaries import facts_of

_ENV = jinja2.Environment(loader=jinja2.FileSystemLoader(str(PROMPTS_DIR)))


def render(template_name: str, **context) -> str:
    """Render a prompt template by name from the shared prompts dir."""
    return _ENV.get_template(template_name).render(**context)


def base_context(deal) -> dict:
    """The frame's variables: the install's config, the operator, and the lead.

    Four of the five come from the install's own config and the operator. The fifth
    is what we know about the person: the facts extracted from the raw
    ``profile_text`` the pipe carried (``leads/summaries.py``), falling back to that
    text itself if the extraction has not run — the same information either way, and
    a lead is never described to the agent as *(none yet)* while the sentences are
    sitting in the row.
    """
    config = SiteConfig.load()
    return {
        "self_name": seller_full_name(),
        "product_docs": config.product_docs or "",
        "campaign_target": config.campaign_target or "",
        "booking_link": config.booking_link or "",
        "profile_summary": _format_profile(deal.lead),
    }


def _format_profile(lead) -> str:
    """The lead's facts as bullets, or the raw profile text, or an admission of nothing."""
    facts = facts_of(lead.profile_summary)
    if facts:
        return "\n".join(f"- {fact}" for fact in facts)
    return lead.profile_text or "(nothing on file)"
