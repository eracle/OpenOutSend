# `outsend` Becomes a Program — Ingest on stdin, and a Command on the PATH

- **Status:** In Progress — **`openoutreach find 50 --json | outsend` is a command now.** The lead
  model exists (`cold_outreach/leads/`), the nine foreign imports are gone, ingest reads stdin and
  upserts idempotently with the door closed behind it, and the program around it is here: a console
  script, this repo's own settings, a SQLite store under `~/.openoutsend/` that migrates itself, and a
  test suite where there was none. **The other half of the clock now runs too**: `outsend send` is one
  bounded pass — read the mail, answer every thread that replied, open what the guards allow — and
  `init` runs implicitly inside it. **What is left is the mailbox**: nothing calls `create_verified`
  and a fresh install has no operator identity to sign with, so a pass says *"no mailbox connected"*
  and does nothing. Then the `openoutreach[send]` extra, which cannot be declared until this
  distribution is published.
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

They install it, and today that is still two lines rather than one — the extra waits on this
distribution being published:

```
pip install openoutreach            # the finder
pip install -e path/to/OpenOutSend  # the sender
```

Then they change their cron entry — the only edit they make all day:

```
openoutreach find 50 --json | outsend
```

Nothing prompts them. `outsend` has never run before, so it creates `~/.openoutsend/`, migrates its
own SQLite, resolves a campaign (a fresh install has none, so it makes one rather than stopping) and
asks the environment for what that campaign needs; the timer has no terminal, so nothing could have
asked a human anyway. It stores fifty rows, prints the campaign it resolved and the count to stderr,
and exits 0.

A week later they run it after a botched export and forty of the fifty rows are ones they already
have. Nothing is duplicated, and the person who unsubscribed on Tuesday stays unsubscribed — their
row is stored and parked, and no re-run lifts it. They do not have to know why; that is the whole
point of the door.

Nothing about the finder changed. `openoutreach find 50 > leads.csv` still prints exactly what it
printed before, because the pipe never asked for a privileged path.

A second cron line, a few minutes later, is what actually mails them:

```
outsend send
```

It reads the mail first, so an opt-out that arrived overnight suppresses the person before anything is
written to them. It answers every thread somebody replied in — no cap, because a reply is not cold
volume. Then it opens as many first emails as the box has headroom, spacing and daylight for, and
exits. **And then it says it did nothing**, because no mailbox is connected yet. That is the sentence
this card still owes.

---

**Single-sentence version:** As an operator, I want `outsend` to be a real command that reads my
finder's output on stdin and stores it safely, so the handover between finding and sending is one pipe
in a cron line rather than a tool I have to drive.

## Where it stands, feature by feature

For anyone picking this up: what an operator can do today, and what they cannot.

| Feature | State | Where it lives |
|---|---|---|
| `outsend` on the PATH, no verb, reads stdin | **Works** | `cold_outreach/__main__.py` |
| Its own store, migrated on first run | **Works** — `~/.openoutsend/data/db.sqlite3`, `OUTSEND_HOME`/`OUTSEND_DB` override | `cold_outreach/settings.py` |
| Idempotent ingest, latest-wins, malformed lines skipped and counted | **Works** | `cold_outreach/leads/ingest.py` |
| Suppression at the door, terminal, re-checked when an address changes | **Works** | `cold_outreach/leads/suppression.py` |
| Campaign resolution by `find`'s rule, narrated to stderr | **Works** | `cold_outreach/leads/campaigns.py` |
| The facts a message is written from | **Works** — extracted lazily on the first email, read by the prompt | `cold_outreach/leads/summaries.py` |
| The transport: SMTP, IMAP sync, mail pass, threads, delivery policy, warmth | **Ported and tested** | `cold_outreach/emails/` |
| `outsend send` — read, answer, open, exit | **Works** | `cold_outreach/send_pass.py` |
| Who is waiting and who wrote back | **Works** — derived from deal state and the mail log's timestamps; no queue table | `cold_outreach/leads/pools.py` |
| `outsend init` | **Half** — collects the campaign's three fields from env or a TTY, and runs implicitly at first send. Does not yet connect a mailbox or record who the operator is | `cold_outreach/__main__.py` |
| **A mailbox** | **Missing.** `create_verified` exists and nothing calls it; `seller_full_name()` reads a Django `User` a fresh install has none of | — |
| `pip install openoutreach[send]` | **Missing** — needs `openoutsend` published first | — |

## What is next, in order

1. **First run has to reach further than a campaign.** A send needs a mailbox (`create_verified`
   exists; nothing calls it) and an operator identity — `seller_full_name()` reads a Django `User`,
   and a fresh install has none, so the agent cannot sign a message. Both belong in `init`, from the
   environment first and a TTY second.
2. **Port the four ignored test files.** Every symbol they reach into the finder for now exists here;
   what is left is the imports and the `tests.` package prefix. They are the send steps' own coverage,
   which the pass leans on.
3. **Publish `openoutsend`**, then declare the extra on the finder's side and grep that nothing under
   `openoutreach/` imports it.
4. **Then the cards that were waiting on all of this** — bounce detection, the inbound silent skip,
   and the plays that replace the one prompt template.

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
      *(**Half done.** The verb exists, reads `OUTSEND_*` first, prompts only on a TTY, errors naming
      the variables when headless, and `outsend send` calls the same thing before any mail moves. What
      it still does not collect is the mailbox and the operator identity — the two things a send needs
      that a campaign does not.)*
- [x] `pip install openoutsend` puts `outsend` on the PATH with its own default SQLite store under
      `~/.openoutsend/`, and the settings module is this repo's rather than a host project's.
      *(Installable and running from a checkout; publishing to PyPI is what the extra below waits on.)*
- [ ] The send path runs against this repo's own tests — there is no harness at all right now.
      *(**Harness built and the pass is covered** — pytest-django, factories for this side's models,
      169 tests green, including the two pool queries, the pass's order, what a failure costs and the
      line it prints. Four inherited files still reach into the finder for its factories and models
      and are ignored by name in `conftest.py`.)*
- [ ] Only then: `openoutreach[send]` is declared on the finder's side, and nothing under
      `openoutreach/` imports `openoutsend`. An extra naming a distribution that does not exist breaks
      the install it exists to simplify.

## What the next slice is

**The mailbox and the operator.** `outsend send` runs and correctly does nothing, because the guards
it obeys have nothing to be free: `Mailbox.objects.create_verified` verifies SMTP auth before storing
a box and no code path calls it, and `seller_full_name()` reads a Django `User` a fresh install has
none of, so the agent has no name to sign with. Both belong in `init`, from the environment first and
a TTY second — the same rule the campaign's three fields already follow.

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
