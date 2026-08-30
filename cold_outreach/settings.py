"""Django settings for the sender — this repo's own, not a host project's.

`cold_outreach` used to be an app inside somebody else's project, which is why the
transport arrived here with no way to start. It has its own settings module now, and
everything an install writes hangs off one root: `state_dir()`, `~/.openoutsend` by
default. The finder made the same choice for the same reason — from a wheel the
package directory is site-packages, and neither a database nor an operator's mail
history belongs there.

**The operator seam is the environment, and most of what it carries seeds the store.**
A variable that only ever configures the process — where the state dir is, which local
clock the sending window is measured in — is read here and lands as a setting. A
variable that answers something a send cannot run without is read once into a row the
operator can then edit: what a message is written from, the mailbox, and the model
and key on `core.SiteConfig`. The split is not tidiness. A setting missing at the point of use is
discovered mid-pass, per lead, with a mailbox already open; a stored answer is checked
before any mail moves.
"""
from __future__ import annotations

import os
from pathlib import Path


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


_DB_PATH = database_path()
_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# There is no web surface: no sessions, no cookies, no password resets, nothing
# signed. The key exists because Django insists on one, and naming that in the value
# is more honest than generating a secret nobody uses and storing it on disk.
SECRET_KEY = os.environ.get("OUTSEND_SECRET_KEY", "outsend-has-no-web-surface")
DEBUG = False

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "cold_outreach.core",
    "cold_outreach.leads",
    "cold_outreach.emails",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(_DB_PATH),
        # WAL, so `outsend` can read while a send pass writes.
        "OPTIONS": {"init_command": "PRAGMA journal_mode=WAL;"},
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Stored UTC, rendered in the operator's own zone wherever a day matters — the send
# cap and the sending window both count from *their* midnight, not the server's.
USE_TZ = True
TIME_ZONE = "UTC"

# ── The operator seam ─────────────────────────────────────────────

# ISO 3166 alpha-2. Resolves the local clock the sending window is measured in.
# A setting rather than a stored answer: it is not a credential, there is nothing to
# verify it against, and a blank one has a defined meaning (UTC) instead of stopping a
# run. The model and its key are the opposite on all three counts, which is why they
# live on `core.SiteConfig` and are collected by `first_run.py`.
OUTSEND_OPERATOR_COUNTRY = os.environ.get("OUTSEND_OPERATOR_COUNTRY", "")
