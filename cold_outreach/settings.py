"""Django settings for the sender — this repo's own, and one host of its apps, not the only.

`cold_outreach` used to be an app inside somebody else's project, which is why the
transport arrived here with no way to start. It has its own settings module now, and
everything an install writes hangs off one root: `defaults.state_dir()`, `~/.openoutsend`
by default. The finder made the same choice for the same reason — from a wheel the
package directory is site-packages, and neither a database nor an operator's mail
history belongs there.

**These apps are hosted twice.** OpenOutreach installs them in one registry beside
OpenOutFind's, which is why the labels are namespaced `outsend_*` — OpenOutFind has its
own `core` — and why what the apps require of a host lives in `defaults.py` and is
splatted here rather than spelled out in two settings modules that can drift.

**The operator seam is the environment, and most of what it carries seeds the store.**
A variable that only ever configures the process — where the state dir is, which local
clock the sending window is measured in — is read here and lands as a setting. A
variable that answers something a send cannot run without is read once into a row the
operator can then edit: what a message is written from, the mailbox, and the model
and key on `outsend_core.SiteConfig`. The split is not tidiness. A setting missing at the
point of use is
discovered mid-pass, per lead, with a mailbox already open; a stored answer is checked
before any mail moves.
"""
from __future__ import annotations

import os

from cold_outreach import defaults

_DB_PATH = defaults.database_path()
_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# There is no web surface: no sessions, no cookies, no password resets, nothing
# signed. The key exists because Django insists on one, and naming that in the value
# is more honest than generating a secret nobody uses and storing it on disk.
SECRET_KEY = os.environ.get("OUTSEND_SECRET_KEY", "outsend-has-no-web-surface")
DEBUG = False

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    *defaults.APPS,
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

# Splatted rather than spelled out, so a name the apps start reading arrives here and in
# OpenOutreach's settings module from one definition instead of two that can drift.
globals().update(defaults.app_settings())
