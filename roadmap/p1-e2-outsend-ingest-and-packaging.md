# `outsend` Becomes a Program — Ingest on stdin, and a Command on the PATH

- **Status:** To Do. **This is the ninth card, and the only one about making the other eight
  runnable.** `cold_outreach/` now imports itself rather than its old home — the transport, the send
  guards, the outreach agent and the mail log all resolve — but nine imports still point at
  OpenOutreach's CRM, and there is no console script, no settings module and no database. Until this
  lands the repo is a library of working parts, and `openoutreach find 50 --json | outsend` is a
  design, not a command.
- **Priority:** High — every other card here describes code that cannot run yet.
- **Effort:** Medium
- **Area:** Packaging + ingest — the receiving end of
  [the boundary contract](https://github.com/eracle/openoutreach-docs/blob/main/roadmap/p1-e2-find-send-boundary-contract.md)

## What

Three things, in this order, because each one unblocks the next:

1. **The lead model** — the nine remaining foreign imports name it. Ingest writes it; the send path
   reads it.
2. **Ingest** — read JSON Lines on stdin, upsert, exit.
3. **The program** — a console script, a settings module, and a default store, so `pip install
   openoutsend` puts `outsend` on the PATH.

## Why the lead model comes first

The port carried the transport across whole but left it reaching back into the finder's CRM for the
row it acts on. Those imports are not a rename anybody forgot — **they are the model this repo has
not designed yet**, and two of them point at code the finder has since deleted:

| Import | Used by | What it has to become here |
|---|---|---|
| `openoutreach.crm.models.DealState` | `steps/send.py`, `steps/reply.py` | the ingested row's own state. The finder's FSM ends at `RESOLVED`; sending starts after that, so the states are this side's to name |
| `openoutreach.crm.models.Deal` | `threads.py`, `models/mailbox.py` | the `(lead_id, campaign)` row ingest writes — the send-cap ledger counts distinct ones |
| `openoutreach.crm.models.Lead` | `sender.py` | the person: address, name, `profile_text` |
| `openoutreach.core.db.leads.suppress_email` | `project.py`, `steps/reply.py` | **the suppression list**, which the contract makes terminal and this side now owns outright |
| `openoutreach.core.db.summaries.materialize_profile_summary_if_missing` | `steps/send.py` | **deleted from the finder.** The extraction from `profile_text` is this repo's, and its own card ([`p1-e2-sender-message-generation`](p1-e2-sender-message-generation.md)) already covers it |
| `openoutreach.core.db.summaries.update_chat_summary` | `steps/reply.py` | same module, same answer |

`openoutreach/emails/` is an **empty directory** in the finder now, and `core/db/summaries.py` is
gone. So these are not imports that would work if the finder were installed — there is nothing on the
other end. Nothing here can be tested until they resolve locally.

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

- [ ] `cold_outreach/` imports nothing from `openoutreach`, and a grep proves it.
- [ ] Ingest reads JSON Lines on stdin, upserts on `(lead_id, campaign)`, skips-and-counts malformed
      lines with a non-zero exit, resolves conflicts latest-wins, and re-checks suppression on an
      address change.
- [ ] Suppression is enforced at the door **and** again at send, since a blank-address row has nothing
      to check on the way in.
- [ ] `outsend` takes no verb, resolves `--campaign` by `find`'s rule, and narrates the resolution to
      stderr. stdout stays clean.
- [ ] `outsend init` exists, collects `product_docs` / `campaign_target` / `booking_link`, and runs
      implicitly at first send — completing without a human on a timer, or stopping with an error that
      names what is missing. An interactive wizard blocking a headless run is the one outcome to avoid.
- [ ] `pip install openoutsend` puts `outsend` on the PATH with its own default SQLite store under
      `~/.openoutsend/`, and the settings module is this repo's rather than a host project's.
- [ ] The send path runs against this repo's own tests — there is no harness at all right now.
- [ ] Only then: `openoutreach[send]` is declared on the finder's side, and nothing under
      `openoutreach/` imports `openoutsend`. An extra naming a distribution that does not exist breaks
      the install it exists to simplify.

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
