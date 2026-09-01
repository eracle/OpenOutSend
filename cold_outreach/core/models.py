# cold_outreach/core/models.py
"""No models. This app is the install's *configuration*, and configuration is not stored.

What used to be here was `SiteConfig`: one pinned row holding the model, its key, and
what the campaign is written from. Every one of those is an answer a human gives, and a
human is not what runs this program — so they come from `OUTSEND_*` on every run, and
`core/config.py` is where they are read and checked.

The app itself stays, because its migrations do: `0002_delete_siteconfig` is how an
existing store loses the table, and an app cannot drop a migration graph a store has
already applied.

**What the pipeline produces is still stored, and lives in the other apps** — the
mailbox's spacing clock and measured capacity (`emails.Mailbox`), the transport log,
the threads, and suppression, which nothing may ever lose.
"""
