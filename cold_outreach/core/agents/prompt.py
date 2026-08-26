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
from cold_outreach.core.operator import seller_full_name

_ENV = jinja2.Environment(loader=jinja2.FileSystemLoader(str(PROMPTS_DIR)))


def render(template_name: str, **context) -> str:
    """Render a prompt template by name from the shared prompts dir."""
    return _ENV.get_template(template_name).render(**context)


def base_context(deal) -> dict:
    """The channel-agnostic prompt variables shared by every outreach entrypoint."""
    campaign = deal.campaign
    return {
        "self_name": seller_full_name(),
        "product_docs": campaign.product_docs or "",
        "campaign_target": campaign.campaign_target or "",
        "booking_link": campaign.booking_link or "",
        "profile_summary": _format_facts(deal.profile_summary),
    }


def _format_facts(summary: dict | None) -> str:
    """Render a `{facts: [...]}` summary blob as a bullet list."""
    facts = (summary or {}).get("facts") or []
    if not facts:
        return "(none yet)"
    return "\n".join(f"- {f}" for f in facts)
