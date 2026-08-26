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

## Status: it runs end to end — install, pipe, connect a box, send

**`outsend` is a command and the store is its own.** Install it, pipe leads in, and they land as rows;
a second, separate invocation is what mails them:

```bash
pip install -e .
outsend init --campaign devtools                            # once: what you sell, who you are, a box
openoutreach find 50 --json | outsend --campaign devtools   # store
outsend send --campaign devtools                            # read, answer, open
```

The two invocations are separate on purpose: a pipe's right-hand side must not block on the network
while a producer is still writing, and the cadences differ — leads arrive when `find` runs, mail moves
on the mailbox's clock. So the cron line is two entries, not one command doing both.

It reads JSON Lines on stdin, upserts on `(lead_id, campaign)`, checks every address against the
suppression list at the door, skips and counts a malformed line, prints the campaign it resolved and
the counts to **stderr**, and exits 0 when every line became a row. Its database is
`~/.openoutsend/data/db.sqlite3` (`OUTSEND_HOME` / `OUTSEND_DB` override it) and it migrates itself on
first run, so a fresh install is an ingest that works rather than a traceback.

**`outsend send` is one bounded pass, not a daemon.** It reads the mail, answers every thread the lead
has replied in, opens as many first emails as the guards allow, and exits — cadence is a timer's job.
Reading first is what makes the other two honest: an opt-out that arrived overnight suppresses the
person before anything is written to them. Openers are the only cold volume, so they are the only
thing under a daily cap, a spacing clock and a sending window; a reply obeys none of the three.
**`outsend init` collects what a first run needs** — what the campaign sells and to whom, the name
that signs the mail, and a mailbox to send it from — and it runs implicitly on the first send, so a
setup step is never something a timer discovers. The environment first, prompts second and **only on a
terminal**; headless, whatever is still missing is one error naming every variable that would have
answered it. The mailbox is stored only once its credentials pass an SMTP login, because the provider
has no health API and that login is the only gate there is.

**Releases are every green push to `main`** (`.github/workflows/deploy.yml`): tests, then a build and a
PyPI upload over trusted publishing, with the version derived from the commit count rather than
committed — the finder's rule, for the reason it went there, since a release nobody has to remember
cannot drift. No token is stored anywhere; the publisher is registered against the workflow filename
and the `pypi` environment, so neither may be renamed.

Still open: arming that (a PyPI pending publisher and the `pypi` environment are two browser steps),
and then `pip install openoutreach[send]`, which can only be declared once this distribution is on
PyPI.

## Tests

```bash
pytest
```

pytest-django against a throwaway state dir (`conftest.py` redirects `OUTSEND_HOME` before Django
loads, so a test run never touches `~/.openoutsend`). Nothing is skipped or ignored: the files that
came across with the transport now assert against this side's own models.

## Layout

| Path | What it is |
| --- | --- |
| `cold_outreach/leads/` | what comes through the pipe — the models, ingest, suppression, the facts extraction |
| `cold_outreach/emails/` | the transport — SMTP, IMAP sync, the mail pass, threads, delivery policy, warmth |
| `cold_outreach/core/` | the outreach agent, its templates, and the sending window |
| `cold_outreach/docs/` | how the agent and its templating work |
| `cold_outreach/settings.py` | this repo's own Django settings and the state dir |
| `cold_outreach/send_pass.py` | one pass — read, answer, open — and the line saying what held it |
| `cold_outreach/first_run.py` | what `init` collects — the campaign's fields, the operator, the mailbox |
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
| `OUTSEND_OPERATOR_NAME` / `OUTSEND_OPERATOR_EMAIL` | who signs the mail, and the address every send is blind-copied to (blank for none) |
| `OUTSEND_MAILBOX_ADDRESS` / `OUTSEND_MAILBOX_PASSWORD` | the box to send from, and its **app password** — a Google box rejects the login password |
| `OUTSEND_SMTP_HOST` / `OUTSEND_SMTP_PORT` / `OUTSEND_IMAP_HOST` / `OUTSEND_IMAP_PORT` | only for a box that is not on Google Workspace; those four default to Gmail's and are never prompted for |
| `OUTSEND_SIGNATURE` | the sign-off appended to every send from that box; empty declines one for good |
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
