"""Test harness — this repo's own, and its first.

**A throwaway state dir.** `cold_outreach.settings` builds its paths at import, so the
root has to be redirected before Django loads or a test run would create
`~/.openoutsend` on the machine running it. pytest-django makes its own test database on
top of that; this is about the directory, not the rows.
"""
import os
import tempfile

os.environ.setdefault("OUTSEND_HOME", tempfile.mkdtemp(prefix="outsend-tests-"))
