[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

# OpenOutSend

The **sending half** of [OpenOutreach](https://github.com/eracle/OpenOutreach). OpenOutreach finds and
qualifies B2B leads and prints them; it does not send email. This is what sends them.

The boundary between the two is a pipe, and nothing else crosses it:

```bash
openoutreach find 50 --json | outsend
```

`find` writes qualified leads as JSON Lines on stdout. `outsend` reads them, stores them in its own
database, and exits — it transmits nothing at that moment. Delivery, pacing and whatever step
structure it grows are its own business, on its own clock.

The pipe is **one-way by design**. Every consumer sees the same bytes, so a file dropped into Instantly
or Smartlead gets exactly what this receiver gets, and "our own sender has no privileged path" is held
by construction rather than by discipline.

## Status: the pipe works; nothing sends yet

**`outsend` is a command and the store is its own.** Install it, pipe leads in, and they land as rows:

```bash
pip install -e .
openoutreach find 50 --json | outsend --campaign devtools
```

It reads JSON Lines on stdin, upserts on `(lead_id, campaign)`, checks every address against the
suppression list at the door, skips and counts a malformed line, prints the campaign it resolved and
the counts to **stderr**, and exits 0 when every line became a row. Its database is
`~/.openoutsend/data/db.sqlite3` (`OUTSEND_HOME` / `OUTSEND_DB` override it) and it migrates itself on
first run, so a fresh install is an ingest that works rather than a traceback.

**What is missing is the other half of the clock.** The transport all exists — SMTP, IMAP sync and the
unsubscribe scan, the mail pass, thread tracking, delivery policy, the sending window, the outreach
agent — but nothing drives it: there is no verb that picks the next deal and sends. Until that lands,
leads arrive and wait.

Also still open: `pip install openoutreach[send]` (the extra can only be declared once this
distribution is published), and four inherited test files that still reach into the finder for the
send-pass pool queries — they are listed by name in `conftest.py` so the list shrinks visibly.

## Layout

| Path | What it is |
| --- | --- |
| `cold_outreach/leads/` | what comes through the pipe — the models, ingest, suppression, the facts extraction |
| `cold_outreach/emails/` | the transport — SMTP, IMAP sync, the mail pass, threads, delivery policy, warmth |
| `cold_outreach/core/` | the outreach agent, its templates, and the sending window |
| `cold_outreach/docs/` | how the agent and its templating work |
| `cold_outreach/settings.py` | this repo's own Django settings and the state dir |
| `cold_outreach/__main__.py` | the `outsend` console script |
| `roadmap/` | open work, mostly inherited from OpenOutreach along with the code it describes |

## Configuration

The environment is the operator seam — the only way in a timer has:

| | |
| --- | --- |
| `OUTSEND_OPERATOR_COUNTRY` | ISO 3166 alpha-2; resolves the local clock the sending window is measured in |
| `OUTSEND_AI_MODEL` | a pydantic-ai `provider:model` id, e.g. `anthropic:claude-sonnet-4-5-20250929` |
| `OUTSEND_LLM_API_KEY` / `OUTSEND_LLM_API_BASE` | credentials for it |
| `OUTSEND_PRODUCT_DOCS` / `OUTSEND_CAMPAIGN_TARGET` / `OUTSEND_BOOKING_LINK` | what a campaign writes from; `outsend init` also asks for these on a terminal |
| `OUTSEND_HOME` / `OUTSEND_DB` | where the store lives |

## The contract it has to implement

The full design — what crosses, the record's field set, exit-code meaning, and why ingest is
idempotent — lives in
[`roadmap/p1-e2-find-send-boundary-contract.md`](https://github.com/eracle/openoutreach-docs/blob/main/roadmap/p1-e2-find-send-boundary-contract.md)
in the `openoutreach-docs` repo. The parts this side owes:

- **Ingest is idempotent**, keyed on `(lead_id, campaign)`, so the pipe is allowed to be lossy and
  recovery is running it again.
- **Suppression is checked at the door and is terminal** — the legal duty came here with `emails/`, and
  a re-ingest must never resurrect somebody who opted out. An address that *changed* is re-checked,
  because ingest is lead-keyed while suppression is address-keyed.
- **Conflicts resolve latest-wins**, field by field: a re-ingest is a correction, not a duplicate.
- **A malformed line is skipped and counted**, named on stderr, with a non-zero exit — `find` spent
  real money on the rows behind it, so aborting the batch throws away paid work.
- **A blank `email` is stored, not rejected.** An exportable row is not a mailable one; the address is
  an enrichment that a later run fills in for free.

## License

MIT — see [LICENSE](LICENSE).
