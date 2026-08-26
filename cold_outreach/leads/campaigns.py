"""Resolving *which* campaign, and filling in what a campaign needs to write with.

The export deliberately carries no campaign column — the finder's own words: *state,
campaign, country, discovery provenance left out rather than dumped in as noise
variables*. So the campaign rides on the **invocation**, and `outsend` resolves it
exactly the way `find` does: `--campaign`, required only when there are several.

The two flags are independent resolutions of the same word. One campaign on each side
under *different names* would resolve silently and file one under the other, which is
why the caller narrates what it resolved to.
"""
from __future__ import annotations

import logging
import os

from cold_outreach.errors import OutsendError
from cold_outreach.leads.models import Campaign

logger = logging.getLogger(__name__)

# The name a first run gets when nobody said otherwise. It exists so that ingest on a
# fresh install cannot fail: an operator piping leads in on day one has not named a
# campaign here, and stopping them to ask would be a step they never knew about.
DEFAULT_CAMPAIGN_NAME = "default"

# What a campaign needs before a message means anything, and the variable each one is
# read from. The environment is the only way in that a timer has.
CONFIG_ENV = {
    "product_docs": "OUTSEND_PRODUCT_DOCS",
    "campaign_target": "OUTSEND_CAMPAIGN_TARGET",
    "booking_link": "OUTSEND_BOOKING_LINK",
}


def resolve_campaign(name: str | None = None) -> Campaign:
    """The campaign this invocation is about.

    Named: that one, created if this is the first time it is mentioned — a campaign is
    a name and three pieces of text, so there is nothing to set up and refusing an
    unknown name would only mean a second command before the first one works.

    Unnamed: the only campaign there is, or a fresh default when there are none.
    Several, and it is an error listing them — ambiguity is never a guess, because the
    wrong answer files a stranger's leads into somebody else's conversation.
    """
    if name:
        campaign, created = Campaign.objects.get_or_create(name=name.strip())
        if created:
            logger.info("campaign %s created", campaign.name)
        return campaign

    campaigns = list(Campaign.objects.order_by("pk")[:2])
    if len(campaigns) == 1:
        return campaigns[0]
    if not campaigns:
        return Campaign.objects.create(name=DEFAULT_CAMPAIGN_NAME)
    names = ", ".join(Campaign.objects.order_by("name").values_list("name", flat=True))
    raise OutsendError(f"several campaigns exist — name one with --campaign: {names}")


def hydrate_from_environment(campaign: Campaign) -> list[str]:
    """Fill this campaign's empty config fields from `OUTSEND_*`. Returns what it wrote.

    Empty fields only: the environment seeds a campaign, it does not overwrite one an
    operator has already edited. Silent and never fatal, because ingest runs behind a
    timer and a message is not being written yet — what is missing surfaces at the
    first send, where it matters.
    """
    written = []
    for field, variable in CONFIG_ENV.items():
        value = (os.environ.get(variable) or "").strip()
        if value and not getattr(campaign, field):
            setattr(campaign, field, value)
            written.append(field)
    if written:
        campaign.save(update_fields=written)
    return written


def missing_config(campaign: Campaign) -> list[str]:
    """The fields a message cannot be written without, and that this campaign lacks.

    `booking_link` is not among them: the prompt renders its whole block only when
    there is one, and a campaign that never offers a call is a normal campaign.
    """
    return [field for field in ("product_docs", "campaign_target") if not getattr(campaign, field)]
