# Outreach Agent

One agent runs the whole email conversation, from the cold open to the last reply. It is doing **Mom Test research, not selling**: the goal of a thread is a candid answer about how the lead works today, not a booked meeting.

**Three touches at most, then the pursuit ends.** The agent opens, and if nobody answers it writes twice more — after three working days, then five — before the deal closes as `unresponsive`. A reply at any point takes the lead out of that sequence for good, and the conversation continues instead.

**The agent never decides *when*.** There is no clock it owns and no `wait` action: the pools in `leads/pools.py` derive who is due from timestamps in the mail log, and the agent is only ever asked what to write.

The cold open, a follow-up and the in-thread replies are the same voice doing the same job, so all three render from **one** prompt (`core/templates/prompts/outreach_agent.j2`), branching on the stage the caller names.

## Flow

```
READY_TO_EMAIL deal                       EMAILED deal whose newest message is inbound
  (cycle row 3, a box is free)              (cycle row 2, outranks opening a new one)
        │                                         │
send_first_email(deal, mailbox)           answer_reply(deal)
  ← emails/steps/send.py                    ← emails/steps/reply.py
        │                                         │
        │                                         ├─ fold the new replies into chat_summary
        │                                         │
        └─────── run_outreach_agent(deal) ────────┘
                 ← core/agents/outreach.py

  emails/mail_pass.py:run_mail_pass — on its own interval: sync mirrors each box into
  the mail log, classify reads the stored bytes, project acts on the reading.
```

Replies outrank openers, and are exempt from the daily cap, the spacing and the working-hours window.

## Decision

`run_outreach_agent` builds context (campaign docs + booking link, the lead's `profile_summary`, and — in thread only — the `chat_summary` plus a recency window of verbatim messages) and makes **one** structured LLM call returning an `OutreachDecision`:

| Action | Effect |
|--------|--------|
| `send_message` | **First touch:** the decision also carries a `subject`; the opener is sent, the thread it opened recorded on the deal, the deal moved to `EMAILED`. **In thread:** threaded reply via `emails/sender.py` (`References` = every Message-ID in the thread, `In-Reply-To` = the last of them), recorded in the mail log as an outbound `Message` in the same thread. The deal stays `EMAILED` — writing our own message is what makes it stop being actionable. |
| `mark_completed` | Close the Deal `COMPLETED` with the agent's `Outcome`. |
| `suppress` | A worded unsubscribe: suppress the person account-wide (`Lead.disqualified`) and move the deal to `UNSUBSCRIBED`. No reply is sent. |

A first touch is constrained to `send_message` with a `subject` (`_validate_opener`) — there is nothing to complete before the thread exists. The prompt is told the thread's age in **working** days (`business_days_between`).

## Summaries

All summary LLM calls go through `core/db/summaries.py` (mem0-style):

- `materialize_profile_summary_if_missing(deal)` builds `profile_summary` before the opener, from the lead's **stored** `profile_text`.
- `update_chat_summary(deal, new_messages, seller_name=…)` folds newly-read replies into `chat_summary` via `reconcile_facts` (mem0 ADD/UPDATE/DELETE/NONE). The mem0 update prompt is vendored under `core/vendor/mem0/`.

The `chat_summary` fact list is where a thread's research value accumulates — free text, not structured fields.

## Prompt

One template, `core/templates/prompts/outreach_agent.j2`: `Strategy` / `Actions` / `Capabilities and honesty` / `Rules`, with the context blocks above them.

`## Strategy` is **two modes**:

- **Discovery** — the default, and where the agent stays. Understand their world without mentioning the product. Carries the standing list of what to steer toward (company/team shape, current workflow, current tooling and its cost, the last thing that broke, the trigger, who else decides), because what we learn here is the point of the thread.
- **Pitching** — entered only on an explicit pull (they ask what you do, how you could help, or for a call). A problem the product solves is *not* a cue to pitch — it's the cue to dig deeper. When it does happen: one or two plain sentences, then back to discovery.

See [Template Variables](./template-variables.md).
