# `outsend` Becomes a Program — Ingest on stdin, and a Command on the PATH

- **Status:** In Progress — **`openoutreach find 50 --json | outsend` is a command now.** The lead
  model exists (`cold_outreach/leads/`), the nine foreign imports are gone, ingest reads stdin and
  upserts idempotently with the door closed behind it, and the program around it is here: a console
  script, this repo's own settings, a SQLite store under `~/.openoutsend/` that migrates itself, and a
  test suite where there was none. **What is left is the other half of the clock** — nothing yet picks
  a deal and sends it, so leads arrive and wait — plus the `openoutreach[send]` extra, which cannot be
  declared until this distribution is published.
- **Priority:** High — every other card here describes code that cannot run yet.
- **Effort:** Medium
- **Area:** Packaging + ingest — the receiving end of
  [the boundary contract](https://github.com/eracle/openoutreach-docs/blob/main/roadmap/p1-e2-find-send-boundary-contract.md)

## What

Three things, in this order, because each one unblocks the next:

1. **The lead model** — the nine foreign imports named it. Ingest writes it; the send path reads it.
2. **Ingest** — read JSON Lines on stdin, upsert, exit.
3. **The program** — a console script, a settings module, and a default store, so `pip install
   openoutsend` puts `outsend` on the PATH.

## Why the lead model came first

The port carried the transport across whole but left it reaching back into the finder's CRM for the
row it acts on. Those imports were not a rename anybody forgot — **they were the model this repo had
not designed yet**, and two of them pointed at code the finder had already deleted. What each one
became:

| Import it was | Used by | What it is now |
|---|---|---|
| `openoutreach.crm.models.DealState` | `steps/send.py`, `steps/reply.py` | `leads.models.DealState` — `READY → EMAILED → COMPLETED / UNSUBSCRIBED`. Named here, and no state is a copy of a finder state: the finder's FSM ends where this one starts |
| `openoutreach.crm.models.Deal` | `threads.py`, `models/mailbox.py` | `leads.models.Deal`, unique on `(lead, campaign)` — a constraint, not a convention, because idempotency is what makes the pipe safe to re-run |
| `openoutreach.crm.models.Lead` | `sender.py` | `leads.models.Lead` — the record's own ten fields plus `profile_text`, keyed on the producer's opaque `lead_id` |
| `openoutreach.core.db.leads.suppress_email` | `project.py`, `steps/reply.py` | `leads.suppression` — an address-keyed table this side owns outright, terminal, and the one thing an erasure must not remove |
| `openoutreach.core.db.summaries.materialize_profile_summary_if_missing` | `steps/send.py` | `leads.summaries` — rebuilt here rather than ported: one structured call, no vendored mem0, and **no campaign conditioning**, which is what made the finder's version tuned for a verdict instead of an opener |
| `openoutreach.core.db.summaries.update_chat_summary` | `steps/reply.py` | same module. The merged fact list comes back whole rather than as mem0's ADD/UPDATE/DELETE events — those existed to address a vector store, and the store here is a JSON list on one row |

Two things that came with the model and were not on the list: `Campaign` (the three pieces of text a
message is written from, which never cross the pipe) and the deletion of `emails/migrations/0008`,
a backfill from the finder's `chat` and `crm` tables that no database on this side has ever had.

## What ingest owes the contract

From the boundary card, and not negotiable on this side:

- **Idempotent, keyed on `(lead_id, campaign)`** — the pipe is allowed to be lossy because recovery is
  running it again.
- **Suppression checked at the door, terminal, never resurrected by a re-ingest.** And re-checked
  whenever an address *changes*, because ingest is lead-keyed while suppression is address-keyed — a
  corrected address is an unsuppressed one unless something looks again.
- **Latest wins, field by field.** A re-ingest is a correction: a lead re-qualified under a changed
  ICP has a new `reason`, and an address filled in by a later enrichment beats the blank it replaces.
- **A malformed line is skipped and counted**, named on stderr, with a non-zero exit. `find` spent real
  money discovering the rows behind it, so aborting the batch throws away paid work — but a silent skip
  is worse than either, because a producer emitting garbage has to be findable.
- **A blank `email` is stored, not rejected.** An exportable row is not a mailable one. Which means the
  door check cannot be the only one: a row with no address has nothing to check, so **suppression is
  enforced again at send**.
- **stdout stays clean.** Everything narrated goes to stderr, including the campaign `outsend` resolved.
- **`outsend` takes no verb**, and `--campaign` is required only when there are several — the same rule
  `find` uses, so one vocabulary spans the pipe.

## User Story

**Persona:** someone who installed OpenOutreach last week, has a CSV habit and a timer, and has just
read that there is a sender now. They have a mailbox, an ICP, and no interest in learning a second
tool's vocabulary.

---

They install it the way the docs say, which is one line and not two:

```
pip install openoutreach[send]
```

Then they change their cron entry — the only edit they make all day:

```
openoutreach find 50 --json | outsend
```

Nothing prompts them. `outsend` has never run before, so it creates `~/.openoutsend/`, migrates its
own SQLite, and asks the environment for what a campaign needs; the timer has no terminal, so nothing
could have asked a human anyway. It stores fifty rows, prints the campaign it resolved and the count
to stderr, and exits 0.

Their mail goes out later, on the sender's own clock, inside the sending window, paced, from their own
box. Nothing about the finder changed. Nothing about the file they used to drop into Instantly is gone
— `openoutreach find 50 > leads.csv` still prints exactly what it printed before, because the pipe
never asked for a privileged path.

A week later they run it after a botched export and forty of the fifty rows are ones they already
sent. Nothing is sent twice, nothing is duplicated, and the person who unsubscribed on Tuesday stays
unsubscribed. They do not have to know why — that is the whole point of the door.

---

**Single-sentence version:** As an operator, I want `outsend` to be a real command that reads my
finder's output on stdin and stores it safely, so the handover between finding and sending is one pipe
in a cron line rather than a tool I have to drive.

## Done when

- [x] `cold_outreach/` imports nothing from `openoutreach`, and a grep proves it. *(The only
      occurrence left in the package is the word in a docstring.)*
- [x] Ingest reads JSON Lines on stdin, upserts on `(lead_id, campaign)`, skips-and-counts malformed
      lines with a non-zero exit, resolves conflicts latest-wins, and re-checks suppression on an
      address change. *(`leads/ingest.py`. An empty value never overwrites a stored one — `null` in a
      record means "never told", which is not a correction of anything.)*
- [x] Suppression is enforced at the door **and** again at send, since a blank-address row has nothing
      to check on the way in. *(`leads/suppression.py` is the list; `emails/sender.suppressed` asks it
      again after the agent has written.)*
- [x] `outsend` takes no verb, resolves `--campaign` by `find`'s rule, and narrates the resolution to
      stderr. stdout stays clean. *(A fresh install with no campaigns resolves to a default rather
      than stopping — ingest on day one cannot fail on a step nobody knew about.)*
- [ ] `outsend init` exists, collects `product_docs` / `campaign_target` / `booking_link`, and runs
      implicitly at first send — completing without a human on a timer, or stopping with an error that
      names what is missing. An interactive wizard blocking a headless run is the one outcome to avoid.
      *(**Half done.** The verb exists, reads `OUTSEND_*` first, prompts only on a TTY, and errors
      naming the variables when headless. "Implicitly at first send" has nothing to hang off yet,
      because nothing sends.)*
- [x] `pip install openoutsend` puts `outsend` on the PATH with its own default SQLite store under
      `~/.openoutsend/`, and the settings module is this repo's rather than a host project's.
      *(Installable and running from a checkout; publishing to PyPI is what the extra below waits on.)*
- [ ] The send path runs against this repo's own tests — there is no harness at all right now.
      *(**Harness built** — pytest-django, factories for this side's models, 132 tests green. Four
      inherited files still reach into the finder for the send-pass pool queries and are ignored by
      name in `conftest.py`; they come back with the send verb.)*
- [ ] Only then: `openoutreach[send]` is declared on the finder's side, and nothing under
      `openoutreach/` imports `openoutsend`. An extra naming a distribution that does not exist breaks
      the install it exists to simplify.

## What the next slice is

**The send verb** — one bounded pass that picks the deals a box may write to right now, runs the
agent, sends, and exits, the way `find` does its own work and stops. It is what brings back
`get_emailable_deals` and `unanswered_replies` (the two pool queries the ignored tests still import
from the finder), and it is where `outsend init` gets its implicit trigger.

## Open questions

- **The erasure path spans two stores.** The finder holds the `Lead` and its `profile_text`; this side
  holds the ingested copy, whatever it extracts from it, the send log and the suppression list. Whose
  command runs an erasure, and whether it is one spanning both or one per side, is unanswered — and
  **the suppression list is the one thing an erasure must not remove**, since forgetting that somebody
  opted out is how they get mailed again.
- **Whether this repo is resident.** It has to read its own mailbox for suppression and for reply
  outcomes, so it needs a cadence either way. The finder ran this experiment: it had a daemon, deleted
  it, and replaced it with one bounded verb behind a systemd timer. The same shape fits here — `outsend`
  ingests, a second bounded verb does one send-and-poll pass — since polling a mailbox is a *cadence*,
  not a residency requirement. Tied to the sequences question, and cheaper to decide together.
- **The operator seam is currently a Django setting** (`OUTSEND_OPERATOR_COUNTRY`, `OUTSEND_AI_MODEL`,
  `OUTSEND_LLM_API_KEY`, `OUTSEND_LLM_API_BASE`), replacing the finder's `SiteConfig` singleton. That is
  the smallest thing that works and it is what `outsend init` will have to write to — decide whether
  init persists them or the environment stays the only way in.
