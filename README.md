[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

# OpenOutSend

The **sending half** of OpenOutreach. [OpenOutFind](https://github.com/eracle/OpenOutFind) finds and
qualifies B2B leads and prints them; this is what sends them. Want both in one install, with one
wizard and one command? That is [OpenOutreach](https://github.com/eracle/OpenOutreach), which requires
both packages and hosts them in one process — this repo stays a program of its own underneath it.

The boundary between the two halves is a pipe, and nothing else crosses it:

```bash
outfind find 50 --json | outsend
```

`find` writes qualified leads as JSON Lines on stdout. `outsend` reads them, stores them in its own
database, and exits — it transmits nothing at that moment. Delivery, pacing and whatever step
structure it grows are its own business, on its own clock.

The pipe is **one-way by design**. Every consumer sees the same bytes, so a file dropped into Instantly
or Smartlead gets exactly what this receiver gets, and "our own sender has no privileged path" is held
by construction rather than by discipline.

## Legal notice

Every message this sends ends with one added line, `Sent with OpenOutreach`, appended after your
signature and opt-out (`cold_outreach/emails/sender.py`). **It is always on — there is no setting to
remove it.** It carries no link, no pixel, and no per-install identifier; nothing reports back whether a
send happened or whether the line survived. This is how the project sustains itself with no
subscription fee, no promo campaign, and no markup on anything you buy through it.

Everything else about a send — the address you write from, what you write, and your compliance with
anti-spam law (CAN-SPAM, GDPR/ePrivacy, CASL, and others) — is yours: this tool provides the mechanism,
not legal cover.

## Status: it runs end to end — install, pipe, connect a box, send

**`outsend` is a command and the store is its own.** Install it, pipe leads in, and they land as rows;
a second, separate invocation is what mails them:

```bash
uv tool install openoutsend             # or pip install -e . from a checkout
outsend check                           # confirm the environment can send from a box
outfind find 50 --json | outsend        # store
outsend send                            # one pass: read, answer, follow up, open
outsend send 5                          # or: keep going until 5 are open
outsend send all                        # or: until nobody is left to email
```

The two invocations are separate on purpose: a pipe's right-hand side must not block on the network
while a producer is still writing, and the cadences differ — leads arrive when `find` runs, mail moves
on the mailbox's clock. So the cron line is two entries, not one command doing both.

It reads JSON Lines on stdin, upserts on `lead_id`, checks every address against the
suppression list at the door, skips and counts a malformed line, prints the counts to **stderr**,
and exits 0 when every line became a row. Its database is
`~/.openoutsend/data/db.sqlite3` (`OUTSEND_HOME` / `OUTSEND_DB` override it) and it migrates itself on
first run, so a fresh install is an ingest that works rather than a traceback.

**`outsend send` is one pass**: it reads the mail, answers every thread the lead has replied in, writes
again to the ones who went quiet, opens as many first emails as the guards allow *right now*, and
exits. Reading first is what makes the rest honest — an opt-out that arrived overnight suppresses the
person before anything is written to them. That is the form a timer wants, and it is what the cron
line fires.

**`outsend send 5` is a goal, and it waits.** Five means five new conversations, and the run keeps
passing until they are open — sleeping out the spacing clock between sends, the daily ceiling, and the
sending window, including overnight and across a weekend if that is what the guards say. Nothing
polls: the pool is asked *when* it could next open one (`Mailbox.objects.next_first_email_at`) and the
run sleeps until then, so the external cycler that used to check whether now was a good time has
nothing left to do. The wait is spent working — every five minutes it wakes for a full pass, so
replies are still answered on a mailbox's cadence while the openers are on hold. Only openers count
toward the goal; a busy inbox cannot satisfy it. It stops short, and says so, when the lead pool is
drained (waiting cannot refill it — ingest is a separate invocation), when no mailbox is connected, or
when two passes in a row fail a send and open nothing, which is a receiver's answer rather than a
clock. Ctrl-C hands back the conversations already opened.

**`outsend send all` makes the pool itself the goal** — everybody who has an address gets one, and the
run ends when nobody is left. It is the same run with one verdict inverted: the drained pool that
*fails* `send 5` at three of five is exactly what `all` asked for, so it exits 0 saying how many went.
Prefer it to typing a count you had to look up, since that number is stale by the time you type it.
The other endings stay failures — no mailbox, and a receiver refusing twice in a row.

So `find N emails --json | outsend && outsend send N` needs no cron entry at all: the second command
returns when the conversations are open, whether that takes four minutes or spans a weekend. A timer
firing a bare `outsend send` remains the other way to run it, and the two do not conflict — the pass is
the same either way, and nothing here is state a lost process would strand.

**A lead who never answers gets two more emails, then the pursuit ends** — after three working days,
then five, and the deal closes as `unresponsive`. A reply at any point ends the sequence and the
conversation takes over. **Follow-ups are cold volume and are treated as such**: they share one daily
cap, one spacing clock and one sending window with the openers, rather than claiming ahead of them out
of a budget of their own. A reply obeys none of the three, because answering someone who wrote to you
is not cold volume and holding the answer until Monday is worse than sending it at 21:00.
**`outsend check` verifies what a run needs** — what this install sells and to whom, the name that
signs the mail, and a mailbox to send it from — and it runs implicitly on every send, so a setup step
is never something a timer discovers. **It comes from the environment and nothing is asked**, on a
terminal as much as headless: this is the right-hand side of a pipe, where a question has nobody to
answer it. Whatever is missing is one error naming every variable that would have satisfied it. The
mailbox is connected only once its credentials pass an SMTP login, because the provider has no health
API and that login is the only gate there is.

**Releases are every green push to `main`** (`.github/workflows/deploy.yml`): tests, then a build and a
PyPI upload over trusted publishing, with the version derived from the commit count rather than
committed — the finder's rule, for the reason it went there, since a release nobody has to remember
cannot drift. No token is stored anywhere; the publisher is registered against the workflow filename
and the `pypi` environment, so neither may be renamed.

The package is on PyPI as **`openoutsend`** — `uv tool install openoutsend`, or
`uvx --from openoutsend outsend check` to try it without installing anything. `openoutreach` pins it
exactly and installs it for you.

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
| `cold_outreach/leads/` | app `outsend_leads` — what comes through the pipe: the models, ingest, suppression, the facts extraction |
| `cold_outreach/emails/` | app `outsend_emails` — the transport: SMTP, IMAP sync, the mail pass, threads, delivery policy, warmth |
| `cold_outreach/core/` | app `outsend_core` — the outreach agent, its templates, the sending window, and the stored site configuration |
| `cold_outreach/docs/` | how the agent and its templating work |
| `cold_outreach/settings.py` | this repo's own Django settings — one host of the apps, not the only one |
| `cold_outreach/defaults.py` | what any host must splat: `APPS`, `state_dir()`, `app_settings()` |
| `cold_outreach/send_pass.py` | one pass — read, answer, open — and the line saying what held it |
| `cold_outreach/send_job.py` | `send N` — passes until the goal is open, waiting out the send clocks |
| `cold_outreach/first_run.py` | what `check` verifies — the message fields, the model, the operator, the mailbox |
| `cold_outreach/core/config.py` | the `OUTSEND_*` a message is written from, read fresh on every run |
| `cold_outreach/__main__.py` | the `outsend` console script |
| `roadmap/` | open work, mostly inherited from OpenOutreach along with the code it describes |

The app labels are namespaced `outsend_*` because these apps are hosted twice: by this
repo's `settings.py`, and by OpenOutreach, which installs them in one registry beside
OpenOutFind's — whose own engine app is also called `core`, and two apps cannot share a
label. So write cross-app model references label-first (`"outsend_emails.Mailbox"`), and
never write a table name as a literal — ask the model (`Mailbox._meta.db_table`).

## Configuration

The environment is the operator seam — the only way in a timer has:

| | |
| --- | --- |
| `OUTSEND_OPERATOR_COUNTRY` | ISO 3166 alpha-2; resolves the local clock the sending window is measured in |
| `OUTSEND_AI_MODEL` | a pydantic-ai `provider:model` id, e.g. `anthropic:claude-sonnet-4-5-20250929` |
| `OUTSEND_LLM_API_KEY` / `OUTSEND_LLM_API_BASE` | credentials for it; pinged once per run before any mail moves |
| `OUTSEND_PRODUCT_DOCS` / `OUTSEND_CAMPAIGN_TARGET` / `OUTSEND_BOOKING_LINK` | what a message is written from; the first two are required |
| `OUTSEND_OPERATOR_NAME` / `OUTSEND_OPERATOR_EMAIL` | who signs the mail, and the address every send is blind-copied to (blank for none) |
| `OUTSEND_MAILBOX_ADDRESS` / `OUTSEND_MAILBOX_PASSWORD` | the box to send from, and its **app password** — a Google box rejects the login password |
| `OUTSEND_SMTP_HOST` / `OUTSEND_SMTP_PORT` / `OUTSEND_IMAP_HOST` / `OUTSEND_IMAP_PORT` | only for a box that is not on Google Workspace; those four default to Gmail's and are never prompted for |
| `OUTSEND_SIGNATURE` | the sign-off appended to every send from that box; empty declines one for good |
| `OUTSEND_HOME` / `OUTSEND_DB` | where the store lives |

**These are read on every run, and none of them is remembered.** Remembering what
somebody typed is a convenience for somebody who types; this program is an agent's, and
an agent supplies its environment every time. So `outsend check` — which `outsend send`
runs implicitly before any mail moves — verifies them and stops with one error naming
every variable that is missing. Two are verified against the provider rather than merely
read: the mailbox by its SMTP login, the model by one ping, neither of which has a health
API. Changing a variable changes the next run, with nothing stale to clear out first.

**Two things are still rows, because neither is an answer somebody gave.** The operator
is a Django `User`, written once — identity, not configuration. And the mailbox keeps
what it *measured*: its spacing clock and the daily capacity it learned from the
provider. Its credentials are seeded from the environment on the run that connects it,
because a box is connected by an accepted SMTP login rather than by a variable.

The store therefore still holds the mailbox's app password.
`~/.openoutsend/data/db.sqlite3` is a file to keep: back it up, and do not copy it around.

## The contract it has to implement

The full design — what crosses, the record's field set, exit-code meaning, and why ingest is
idempotent — lives in
[`roadmap/p1-e2-find-send-boundary-contract.md`](https://github.com/eracle/openoutreach-docs/blob/main/roadmap/p1-e2-find-send-boundary-contract.md)
in the `openoutreach-docs` repo. The parts this side owes:

- **Ingest is idempotent**, keyed on `lead_id`, so the pipe is allowed to be lossy and
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
