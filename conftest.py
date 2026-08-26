"""Test harness — this repo's own, and its first.

Two jobs, both about not touching anything real:

- **A throwaway state dir.** `cold_outreach.settings` builds its paths at import, so
  the root has to be redirected before Django loads or a test run would create
  `~/.openoutsend` on the machine running it. pytest-django makes its own test
  database on top of that; this is about the directory, not the rows.
- **Naming what does not run yet.** The inherited email tests came across with the
  transport and still reach into the finder for its factories and models. They are
  ignored *by name*, so the list shrinks visibly as they are ported rather than a
  green run hiding them.
"""
import os
import tempfile

os.environ.setdefault("OUTSEND_HOME", tempfile.mkdtemp(prefix="outsend-tests-"))

# Each of these imports `openoutreach.*` — `LeadFactory`/`DealFactory`, `DealState`,
# `SiteConfig`, `suppress_email`. Every one of those now exists on this side, so what
# is left is rewriting the imports and the `tests.` package prefix; the pool queries
# they used to reach for are `cold_outreach/leads/pools.py`. Delete a line when its
# file runs.
collect_ignore = [
    "cold_outreach/tests/emails/test_mail_pass.py",
    "cold_outreach/tests/emails/test_reply.py",
    "cold_outreach/tests/emails/test_send.py",
    "cold_outreach/tests/emails/test_unsubscribe.py",
    "cold_outreach/tests/test_sending_window.py",
]
