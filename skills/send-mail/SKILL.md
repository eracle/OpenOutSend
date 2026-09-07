---
name: send-mail
description: Send cold outreach with OpenOutSend — pipe leads in with `outfind find --json | outsend`, then run `outsend send` to mail them under the built-in guards. Use when the user wants to send, follow up on, or check the status of an outreach campaign that already has leads. Never send unasked — this puts mail in strangers' inboxes under the user's own identity.
user-invocable: true
argument-hint: [N|all]
---

# Sending mail with OpenOutSend

OpenOutSend is a self-hosted CLI sender — the right-hand side of a pipe. It stores leads a finder
piped in, writes the opener with an LLM, and sends from a connected mailbox under three guards it
owns: measured warm capacity, per-box send pacing, and the operator's own sending window.

**It finds nobody.** Leads arrive already qualified from [OpenOutFind](https://github.com/eracle/OpenOutFind)
(or [OpenOutreach](https://github.com/eracle/OpenOutreach), which bundles both). If the user wants
leads, that is a different tool — use the `find-leads` skill instead.

**Never send unasked.** Every send puts mail in a stranger's inbox under the user's own identity and
mailbox. Checking status, ingesting leads and running `check` are safe to do proactively; `send` is
not — only run it when the user has asked to send, follow up, or continue a campaign.

## Is it installed, and is it ready?

```bash
outsend check          # verifies the environment can send: message, model, operator, mailbox
```

`check` reads only `OUTSEND_*` from the environment — there is no config row and nothing is asked
interactively. It fails cheaply with one line naming every variable still missing, grouped as what
you sell (`OUTSEND_PRODUCT_DOCS`/`OUTSEND_CAMPAIGN_TARGET`), the model
(`OUTSEND_AI_MODEL`/`OUTSEND_LLM_API_KEY`), who signs the mail
(`OUTSEND_OPERATOR_NAME`/`OUTSEND_OPERATOR_EMAIL`), and the mailbox
(`OUTSEND_MAILBOX_ADDRESS`/`OUTSEND_MAILBOX_PASSWORD`, an **app password**, not the login password).
`send` runs the same check implicitly before any mail moves, so `check` is for diagnosing *why*
nothing sent, not a required first step.

If the command is missing, run it through `uvx` instead — `uvx openoutsend check` — or install it
with `pip install openoutsend`.

**Never accept anything on the user's behalf.** If a required variable is unset, say so and let the
user set it; do not export credentials or invent a product description for them.

## Getting leads in

```bash
outfind find 50 --json | outsend
```

`outsend` with no verb reads JSON Lines on stdin, stores each row keyed on `lead_id`, checks every
address against the suppression list at the door, and exits. It transmits nothing at that moment —
ingesting and sending are separate invocations, because a pipe's right-hand side must not block on
the network while a producer is still writing.

- **Idempotent, keyed on `lead_id`.** Running the same pipe twice is a correction (latest field wins),
  never a duplicate — re-piping after a partial failure is always safe.
- **A malformed line is skipped and counted**, named on stderr, non-zero exit — the rows behind it
  cost the finder real money, so the batch is never aborted over one bad line.
- **A blank `email` is stored, not rejected.** An address is an enrichment a later re-ingest can fill
  in for free.

## Sending

```bash
outsend send                 # one pass: read the mail, answer replies, open what the guards allow now
outsend send 5                # keep passing until 5 conversations are open, waiting out the send clocks
outsend send all               # until nobody is left to email
```

A pass, in order: read the mailbox (an opt-out overnight suppresses someone before anything is
written to them), answer every thread the lead replied in, follow up on ones gone quiet, then open as
many first emails as the guards allow *right now*. Replies and follow-ups ignore the daily cap and
spacing clock; **openers are the only cold volume**, so they are the only thing capped.

`outsend send 5` waits — sleeping out the spacing clock, the daily ceiling and the sending window
between passes, including overnight or across a weekend if that is what the guards say. `outsend send
all` makes the pool itself the goal and exits 0 once nobody is left, even though the same drained pool
would report `send 5` as falling short at three of five — it is the same ending with one verdict
inverted, so prefer `all` to typing a count that goes stale as you type it.

**A lead who never answers gets two more emails, then the pursuit ends** (after three working days,
then five); a reply at any point ends the sequence and the conversation takes over instead.

## Answering the opener yourself, with no second LLM key

If you (the calling agent) are already reasoning about this lead in the conversation,
`OUTSEND_LLM_API_KEY` is a second, redundant LLM bill for an opener you can write yourself. Add
`--agent-draft` and the pass stops at the first deal needing one instead of calling `AI_MODEL` — and
in this mode `check`/`send` never require `OUTSEND_AI_MODEL`/`OUTSEND_LLM_API_KEY` at all:

```bash
outsend send --agent-draft
```

It exits non-zero with `draft_pending`, carrying the deal's own fields on the error object under
`--json` (`{"error": {"type": "draft_pending", "profile_text", "company", "title", "full_name",
"lead_id"}}`, or on the plain-text line without `--json`). Write the opener the same way you would
write anything else in this conversation, then re-run the **same command** with your answer attached:

```bash
outsend send --agent-draft --subject "Quick one" --body "Saw you're hiring for platform roles..."
```

This resumes exactly that deal — no id to track, since at most one is ever pending — and sends it the
moment a mailbox is free. **An answer already given is kept, not thrown away**, if the spacing clock
or the sending window is holding every box right now; a later bare `outsend send --agent-draft` (no
`--subject`/`--body`) will pick it back up and send it once a box frees up, so you never have to
re-supply the same text. No count yet — `--agent-draft` is one pass at a time, like a bare `outsend
send`.

**Don't pass `--agent-draft` unless you intend to answer every `draft_pending` it raises** — a bare
`outsend send` uses `AI_MODEL` and writes every opener itself, which is the right default for a run
nobody is driving turn-by-turn. `--subject` and `--body` are answered together; passing one without
the other, or either without `--agent-draft`, is refused before any mail moves.

## Reading the output

Nothing here prints a CSV — sending has no rows to hand on. Everything is narration on **stderr**:
counts (`read 2 new message(s) · answered 1 · followed up 0 · opened 3`), the gate holding a pass that
did nothing (*"no mailbox connected, so nothing can be sent"* rather than a silent zero), and, for a
goal, how the run ended (`opened 5 of 5 conversation(s) in 3 pass(es)`, or `stopped at ...` with the
wall it hit). **Never report a pass that did nothing as "no leads to send"** — read the holding line;
it names the actual gate (no mailbox, an empty pool, the daily ceiling, outside the sending window).

## Exit codes and failures

Non-zero means something needs attention; a goal that fell short still opened whatever it could
before saying so. Failures are one line on stderr:

```
error: <message>
```

`draft_pending` is the one exception with a stable `type` and, under `--json`, a payload — see above.
Every other failure is plain text naming what is wrong (a missing variable, a rejected model key, a
mailbox that failed its SMTP login); there is no other typed vocabulary to branch on yet.

## Things not to do

- **Never run `outsend send` (or `run`, from OpenOutreach) unless the user asked to send.** Checking
  status and ingesting leads are safe; putting mail in someone's inbox is not something to do on
  spec.
- Don't invent a daemon or a watch loop — a pass reads what the guards allow *right now* and exits;
  `send N`/`send all` are how you wait for more without polling.
- Don't pass `--agent-draft` unless you plan to answer every `draft_pending` it raises.
- Don't parse stderr for data — there is none here to parse; `outfind`'s CSV/JSON is the data surface.
- Don't try to remove or word around the `Sent with OpenOutreach` attribution line — it is always on,
  by design (see the repo's Legal notice), and not a setting this CLI exposes.
