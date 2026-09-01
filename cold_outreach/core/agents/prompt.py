# cold_outreach/core/agents/prompt.py
"""Jinja plumbing + the shared context for the outreach prompt.

One template renders both the cold open and every in-thread reply
(``outreach_agent.j2``); ``base_context`` is the half of its variables that
doesn't depend on where in the thread we are. ``core.agents.outreach`` adds the
conversation half.
"""
from __future__ import annotations

import jinja2

from cold_outreach.core.conf import PROMPTS_DIR
from cold_outreach.core.config import SiteConfig
from cold_outreach.core.operator import seller_full_name
from cold_outreach.leads.summaries import facts_of

_ENV = jinja2.Environment(loader=jinja2.FileSystemLoader(str(PROMPTS_DIR)))


def render(template_name: str, **context) -> str:
    """Render a prompt template by name from the shared prompts dir."""
    return _ENV.get_template(template_name).render(**context)


def base_context(deal) -> dict:
    """The channel-agnostic prompt variables shared by every outreach entrypoint.

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


def _format_facts(summary: dict | None) -> str:
    """Render a ``{"facts": [...]}`` blob as a bullet list."""
    facts = facts_of(summary)
    if not facts:
        return "(none yet)"
    return "\n".join(f"- {fact}" for fact in facts)
