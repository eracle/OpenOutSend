# cold_outreach — ported from OpenOutreach, not yet wired up

This directory is **parked code**, and it is now the whole of this repo. Nothing imports it,
nothing runs it, and it is not on `INSTALLED_APPS`. It is the cold-outreach half of
[OpenOutreach](https://github.com/eracle/OpenOutreach), moved here whole so it can be integrated
deliberately rather than rewritten from memory.

## Why it moved

OpenOutreach became a **lead finder**: product description in, qualified leads out, with the reason
each one was chosen, handed over as CSV. Sending is a specialism it was not good at, and a good lead
list ruined by a bad opener reads to the buyer as a bad lead list. So the sending half comes here,
where the whole job is the message.

The boundary between the two is **one-way and public**: OpenOutreach exports leads, and nothing flows
back. Whatever this code becomes, it must read that same export — no shared model, no direct import,
no DB join. OpenOutSend gets no privileged path that Instantly or Smartlead would not get.

The design decisions behind that split live in `roadmap/p1-e3-leadfinder-sequencer-boundary.md` in the
`openoutreach-docs` repo. Read it before integrating; several decisions were reversed after the fact
and the reasoning is recorded there rather than in either repo's git history.

## What is here

Layout mirrors OpenOutreach's, so the original import paths still read correctly.

| path | what it is |
|---|---|
| `emails/models/mailbox.py` | `Mailbox` — one SMTP sending inbox, plus the pool-level pacing manager (`free_for_first_email`, `remaining_today`) and the measured daily cap. |
| `emails/models/maillog.py` | `Message` / `Thread` / `DeliveryEvent` / `FolderCoverage` — the mail log. The record and its interpretation are separate objects; `processed_at IS NULL` is the third state. |
| `emails/sync.py`, `classify.py`, `project.py`, `mail_pass.py` | The mail pass, in its three ordered jobs: sync (IMAP → rows, the only network step), classify (pure, versioned, over stored bytes), project (bounces → `DeliveryEvent`, opt-outs → suppression). |
| `emails/threads.py` | Union-find over Message-IDs, so a reply carrying only `In-Reply-To` still reaches its deal. |
| `emails/parsing.py` | Message parsing helpers. |
| `emails/sender.py`, `smtp.py` | The send path: body → signature → opt-out line → attribution, `List-Unsubscribe`, SMTP auth. |
| `emails/warmth.py`, `delivery_policy.py`, `report.py` | The three send guards — hours, rate, volume — and the receiver feedback that moves them. The daily cap is **measured** from the box's own Sent folder, not configured. |
| `emails/steps/send.py`, `steps/reply.py` | The two pipeline steps: send one opener, answer one reply. |
| `emails/admin.py`, `apps.py`, `migrations/` | Django wiring and the full migration history of the `emails` app. |
| `core/agents/outreach.py`, `prompt.py` | The single outreach agent that owns the whole thread — cold open and every reply. Its job is **Mom Test research, not selling**: learn how the lead works today, never pitch unprompted. |
| `core/templates/prompts/outreach_agent.j2` | That agent's prompt. |
| `core/sending_window.py` | Mon–Fri, 08:00–20:00 in the *operator's* timezone, derived from their onboarding country. Openers wait for it; replies never come through it. |
| `tests/` | The suite for all of the above, moved unchanged. |

## What it still expects, and does not find here

Every one of these is an OpenOutreach import that did **not** come along. They are the integration
work, and most of them are the boundary in disguise:

| import | what it was | what it has to become |
|---|---|---|
| `openoutreach.crm.models` (`Deal`, `Lead`, `DealState`) | the lead, the campaign-scoped deal, the state machine | **the port's main question.** A sequencer's unit is a recipient in a sequence, not a `Deal` in a finder's FSM. Do not recreate the finder's states here — take the exported row (`email, first_name, last_name, company, title, website, linkedin_url, reason, lead_id`) as the input and model the rest yourself. `lead_id` is the finder's join key; carry it, don't resolve it. |
| `openoutreach.core.db.leads` (`suppress_email`) | permanent, account-level opt-out on `Lead.disqualified` | **the opt-out duty moves with this code.** A finder that never contacts anyone is not the sender under CAN-SPAM / GDPR / CASL, so the `List-Unsubscribe` header, the `+unsub` alias, the mailbox scan and the agent's `suppress` action are all this side's obligation now. Suppression needs a home here. |
| `openoutreach.core.db.deals`, `db.summaries` | deal creation and conversation summaries | conversation state, so it belongs on this side; re-model against whatever replaces `Deal`. |
| `openoutreach.core.llm` | the pydantic-ai model factory (`SiteConfig.ai_model` + key) | needs an equivalent, or accept an injected agent. |
| `openoutreach.core.models.SiteConfig` | the config singleton — `country_code` drives the sending window | `sending_window.py` reads it directly; give it a seam rather than a singleton. |
| `openoutreach.core.conf` | `WARM_*`, `MIN_SEND_INTERVAL_SECONDS`, `SEND_WINDOW_*` | copy the constants across; `WARM_CEILING_SENDS` is **derived** from the window and the pacing, so keep the arithmetic rather than re-declaring a number. |
| `openoutreach.core.business_time` | Mon–Fri, used by the sending window and by thread age | small and self-contained; copy it. |
| `openoutreach.core.operator` | the operator `User` — the BCC target on their own campaigns | an account concept this app already has in some form. |
| `openoutreach.core.cycle` | the daemon loop that called these steps | **do not port.** The loop is the finder's, and it deleted its own daemon in favour of one bounded verb behind a timer. Sending here wants a cadence, not a resident process. |
| `openoutreach.core.logblock` | log formatting | cosmetic; drop or replace. |

## Two things not to lose

**The freemium promotional campaign does not come with this code.** OpenOutreach shipped a campaign
advertising itself, sent from the operator's own mailbox. It has been reclassified from growth asset
to install tax and dies with the sender — it should not reappear here. `LEGAL_NOTICE.md` §4 in
OpenOutreach is the disclosure that describes it.

**The measured send cap is the good part.** `warmth.py` reads the box's own Sent folder and derives a
daily ceiling from what that mailbox already does, rather than taking a configured number. It replaced
a declared 50/day that nobody had measured. Whatever this becomes, keep the principle: the receiver
counts every message the box emits, so the ledger has to count the same things.
