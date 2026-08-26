# cold_outreach/core/operator.py
"""Who is running this sender.

Self-hosted means one operator, so identity is a lookup, not a parameter. The name
signs the mail and binds the outreach agent's persona; the country decides which
local clock the sending window is measured in.

The country is a **setting**, not a config singleton — this side has no site-config
table to load, and a single value read at the point of use is the smaller seam.

Nothing is cached across calls. The read is a single indexed row and happens at most
once per pass; a cache would only add a way for a renamed operator to keep signing
emails with their old name until the process restarts.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def get_active_user():
    """The Django ``User`` running the sender."""
    from django.contrib.auth.models import User

    return User.objects.filter(is_active=True, is_staff=True).order_by("pk").first()


def operator_country() -> str:
    """The operator's ISO 3166 alpha-2 country code, or ``""`` when unset."""
    from django.conf import settings

    return getattr(settings, "OUTSEND_OPERATOR_COUNTRY", "") or ""


def seller_name() -> str:
    """The seller's first name as the LLM knows it, with a username fallback."""
    user = get_active_user()
    return (user.first_name or "").strip() or user.username


def seller_full_name() -> str:
    """The seller's full name for the prompt's identity binding."""
    user = get_active_user()
    full = f"{user.first_name or ''} {user.last_name or ''}".strip()
    return full or user.username
