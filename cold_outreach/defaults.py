"""What a host project must put in its settings for `cold_outreach`'s apps to run.

This repo's own `settings.py` is one host; OpenOutreach, which installs these apps in
its registry next to OpenOutFind's, is another. Both splat `app_settings()` rather than
each spelling the names out, so a name the apps start reading arrives in both from one
definition instead of two that can drift.

Only what the *apps* read belongs here. `SECRET_KEY`, `DATABASES` and the rest are the
host's own business — as is `AUTH_USER_MODEL`, which `emails/models/maillog.py` reads
but which every Django project already has; the requirement it does carry is that
`django.contrib.auth` be installed.
"""
from __future__ import annotations

import os
from pathlib import Path

#: The apps, in dependency order, for a host's `INSTALLED_APPS`. `django.contrib.auth`
#: must be installed too — `emails.Message` points at `AUTH_USER_MODEL`.
APPS = [
    "cold_outreach.core",
    "cold_outreach.leads",
    "cold_outreach.emails",
]


def state_dir() -> Path:
    """The root everything this install writes hangs off.

    `OUTSEND_HOME` overrides it, which is what makes a throwaway store possible
    without a second copy of every path below it.
    """
    return Path(os.environ.get("OUTSEND_HOME") or Path.home() / ".openoutsend").expanduser()


def database_path() -> Path:
    """The SQLite file. `OUTSEND_DB` names another one — for a scratch run, or a second store."""
    override = os.environ.get("OUTSEND_DB")
    return Path(override).expanduser() if override else state_dir() / "data" / "db.sqlite3"


def app_settings() -> dict:
    """The settings names `cold_outreach`'s apps read, for a host to splat into its module."""
    return {
        # ISO 3166 alpha-2. Resolves the local clock the sending window is measured in.
        # A setting rather than a stored answer: it is not a credential, there is nothing
        # to verify it against, and a blank one has a defined meaning (UTC) instead of
        # stopping a run. The model and its key are the opposite on all three counts,
        # which is why they live on `outsend_core.SiteConfig` and `first_run.py` collects
        # them.
        "OUTSEND_OPERATOR_COUNTRY": os.environ.get("OUTSEND_OPERATOR_COUNTRY", ""),
    }
