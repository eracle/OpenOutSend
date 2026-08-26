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

## Status: parked code, not yet a program

Everything in `cold_outreach/` came from OpenOutreach whole, so it would be integrated deliberately
rather than rewritten from memory. **The transport exists** — SMTP, IMAP sync and the unsubscribe scan,
the mail pass, thread tracking, delivery policy, sending windows, the outreach agent and its prompt
template, and eight migrations. What does not exist yet is the program around it:

- no console script, so there is no `outsend` on the PATH;
- no standalone settings — the app expects a host project's `INSTALLED_APPS`;
- no default store, where the finder has its own `state_dir()`;
- no ingest: nothing yet reads stdin.

Until those land this repo is a library of working parts and a roadmap, and `pip install openoutsend`
is not yet a thing you can type.

## Layout

| Path | What it is |
| --- | --- |
| `cold_outreach/emails/` | the transport — SMTP, IMAP sync, the mail pass, threads, delivery policy, warmth |
| `cold_outreach/core/` | the outreach agent, its templates, and the sending window |
| `cold_outreach/docs/` | how the agent and its templating work |
| `roadmap/` | open work, mostly inherited from OpenOutreach along with the code it describes |

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
