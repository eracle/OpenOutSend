# Outreach Agent

One agent writes every email OpenOutSend sends, from the cold open to the last reply. In cold outreach it is doing **Mom Test research, not selling**: the goal of a thread is a candid answer about how the lead works today, not a booked meeting.

**Every message is the same call.** The frame (`core/templates/prompts/frame.j2`) is the system prompt, the whole history with this lead (`core/agents/history.py`) comes next, and the prompt for the email being written comes last. An opener, a follow-up and a reply differ only in that last prompt, and each lives with the step that sends it.

**The agent never decides *when*.** There is no clock it owns and no `wait` action: the planner in `planner.py` derives who is due from timestamps in the mail log, and the agent is only ever asked what to write.

## Flow

```
first step due                    later step due                 follow-up due                  an unanswered reply
  (a box is free)                   (its days since the last)      (silence, gap elapsed)         (outranks all)
        │                                 │                              │                              │
send_first_email(deal, box,       send_next_step(deal,           send_follow_up(deal)           answer_reply(deal)
                 step, line)                     step, line)
  ← emails/steps/send.py            ← emails/steps/send.py         ← emails/steps/follow_up.py    ← emails/steps/reply.py
  prompt: the opening step file     prompt: that step's file       prompt: FOLLOW_UP_PROMPT       prompt: REPLY_PROMPT
        │                                 │                              │                              │
        └─────────────────────── run_outreach_agent(deal, prompt, *, opener) ─────────────────────────┘
                      ← core/agents/outreach.py
                      system: frame.j2 · user: history + "Write this email now" + prompt

  emails/mail_pass.py:run_mail_pass — on its own interval: sync mirrors each box into
  the mail log, classify reads the stored bytes, project acts on the reading.
```

`send_pass.py` answers replies first, then sends openers, later steps and follow-ups as one queue, oldest due first (`planner.cold_sends_due`), so no kind can starve another. A reply is exempt from the daily cap, the spacing and the sending window. Every step and follow-up shares one budget, except that a Resend box's daily cap is spent per sequence.

## The three prompts

- **A sequence step**: a step file (`core/sequences.py`). An `.email.j2` step never reaches the agent: it is sent as written. Cold's only step is `sequences/cold/E0-opener.prompt.j2`, which carries the cold rules (75 words, no link, no meeting request, sourced claims only) and draws the move from the prompt-line pool with `{{ prompt_line }}`.
- **A follow-up**: `FOLLOW_UP_PROMPT` in `emails/steps/follow_up.py`. It is sent only when `OUTSEND_FOLLOW_UPS` is on (the default), after `FOLLOW_UP_GAPS_BUSINESS_DAYS = (3, 5)` of silence, and only if it lands before the deal's next step is due. It sees the opener's prompt in the history, so it keeps the opener's move without the prompt line being read back to it.
- **A reply**: `REPLY_PROMPT` in `emails/steps/reply.py`, carrying the two modes, Discovery (the default, and where the agent stays) and Pitching (only on an explicit pull).

**The rules live in the prompts.** The frame carries none, so a step that needs a link says so, and cold's word ceiling sits in cold's own prompts. There is no validation of content after the fact and no retry; a draft is sent as written.

## The history

Every turn in every thread of every deal the lead is in, oldest first and verbatim, with no cap:

```
[2026-10-01] PROMPT YOU WERE GIVEN:   the step's prompt, as rendered when it was sent
[2026-10-01] YOU SENT (subject "…"):  the email, as sent
[NO REPLY for 3 business days]
[2026-10-06] PROMPT YOU WERE GIVEN:   the follow-up prompt
[2026-10-06] YOU SENT (subject "…"):
[2026-10-07] THEY WROTE:              their reply, quoted history stripped
```

Each send records its rendered prompt on the `Message` (`prompt`, `prompt_path`, `step`), so the history replays what the model was actually asked even after a step file is edited. Bounces and out-of-office mail are facts about the thread, not turns, and never reach the model.

## Decision

`run_outreach_agent` makes **one** structured LLM call returning an `OutreachDecision`:

| Action | Effect |
|--------|--------|
| `send_message` | **First touch:** the decision also carries a `subject`; the opener is sent, the thread it opened recorded on the deal, the deal moved to `EMAILED`. **In thread:** threaded reply via `emails/sender.py` (`References` = every Message-ID in the thread, `In-Reply-To` = the last of them), recorded in the mail log as an outbound `Message` in the same thread. |
| `mark_completed` | Close the Deal `COMPLETED` with the agent's `Outcome`. |
| `suppress` | A worded unsubscribe: put the address on the account-wide list and close the deal `COMPLETED` with the `unsubscribed` outcome. No reply is sent. |

**`suppress` and the `unsubscribed` outcome are one decision the model can word two ways**, and `ends_in_an_opt_out` reads them as one: a decision naming that outcome while reaching for `mark_completed` is honoured as a suppression. Both `answer_reply` and `send_follow_up` ask that first, before branching on the action.

An opener (`opener=True`) must be `send_message` with a `subject` (`_validate_opener`); there is nothing to complete before the thread exists.

## What the agent knows about the lead

`leads/summaries.py:materialize_profile_summary_if_missing(lead)` extracts a fact list from the lead's stored `profile_text` once, before the opener. The frame shows those facts, or the raw text if the extraction has not run. The history replaces any summary of the conversation itself.

See [Template Variables](./template-variables.md).
