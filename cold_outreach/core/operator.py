# cold_outreach/core/operator.py
"""Who is running this sender.

Self-hosted means one operator, so identity is a lookup, not a parameter. The name
signs the mail and binds the outreach agent's persona; the country decides which
local clock the sending window is measured in.

The country stays a plain **setting**: it is not a credential, there is nothing to
verify it against, and a blank one has a defined meaning (UTC) rather than a reason to
stop a run. What `check_ready()` refuses to start without is the opposite of all three.

The operator's name and address are a **row**, not a variable — the Django `User` both
children read. It is identity rather than configuration: written once, and not re-read
from the environment on later runs, so renaming a box does not rename the person.

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


def set_operator(*, full_name: str, email: str = ""):
    """Record who this install sends as, and return them.

    One operator means an upsert of a single row rather than a user table: the name
    signs the mail and binds the agent's persona, the address is the blind copy on every
    send. A first name alone is a complete answer — plenty of cold email is signed with
    one.

    ``username`` is Django's required handle and nothing authenticates with it; there is
    no web surface to log into. It is written once, when the row is created, so renaming
    the operator never has to move a key.
    """
    from django.contrib.auth.models import User

    first, _, last = full_name.strip().partition(" ")
    user = get_active_user() or User(
        username=(email or full_name).strip()[:150], is_staff=True, is_active=True)
    user.first_name, user.last_name, user.email = first, last.strip(), email
    user.save()
    return user


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
