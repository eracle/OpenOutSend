# Outreach Agent

The model is asked for two things, and they are two different calls:

- **The words of a step.** A step written from a prompt (`.prompt.j2`) is one call that returns an email and nothing else. It decides nothing.
- **One turn of a conversation.** Once a sequence's last step has gone, its deal rests in `conversing`, and a reply is answered by a turn: the model reads the whole thread and either answers or ends the conversation with a reason.

In cold outreach both are doing **Mom Test research, not selling**: the goal of a thread is a candid answer about how the lead works today, not a booked meeting.

**The model never decides *when*.** Which step a deal is owed is its state, and when it is due is that state's wait (`leads/lifecycle.py`). There is no clock the model owns and no `wait` action.

## Flow

```
awaiting_E<k>  ── the step's day comes ──▶  sending_E<k>  ── a box is free ──▶  awaiting_E<k+1> … conversing
                                                 │                                                  │
                                    send_step(deal, step, plan)                        a turn (a message waits)
                                      ← emails/steps/send.py                             ← machines/handlers.py
                                                 │                                                  │
                          .email.j2: sent as written, no model call               the model reads the thread and the
                          .prompt.j2: write_email(deal, prompt, opener=)          conversation prompt, returns a DealTurn
                            ← core/agents/outreach.py                               ← leads/conversing.py
                            system: frame.j2                                        to=conversing + message: sent, stays
                            message_history: the history                            to=completed + outcome: nothing sent
                            user: "Write this email now" + prompt

  emails/mail_pass.py:run_mail_pass — on its own interval: sync mirrors each box into
  the mail log, classify reads the stored bytes, project acts on the reading.
```

`send_pass.py` runs out the waits, takes a turn in every conversation with a message waiting, then sends the due steps as one queue, longest due first (`planner.sends_due`). An answer in a conversation is exempt from the daily cap, the spacing and the sending window. Every step shares one budget, except that a Resend box's daily cap is spent per sequence.

**A reply never stops a step.** A sequence's steps are written for a list, so each goes on its day whether or not the lead wrote back. A reply that arrives during the steps is waiting in `conversing` when the deal gets there. The one thing code honours before then is the opt-out: a reply whose own text carries the footer's word (`stop`) suppresses the address when the mail pass reads it (`emails/project.py`).

## The prompts

- **A sequence step**: a step file (`core/sequences.py`). An `.email.j2` step never reaches the model: it is sent as written. Cold's only step is `sequences/cold/E0-opener.prompt.j2`, which carries the cold rules (75 words, no link, no meeting request, sourced claims only) and draws the move from the prompt-line pool with `{{ prompt_line }}`.
- **A conversation**: the frame, then the folder's own `conversation[-<days>d].prompt.j2`, rendered with the deal's fields. A folder without one gets `BUILT_IN_PROMPT` in `leads/conversing.py`, written for cold outreach: two modes, Discovery (the default, and where the model stays) and Pitching (only on an explicit pull). `<days>d` is how many business days of silence close the conversation; without it, `CONVERSATION_CLOSES_AFTER_BUSINESS_DAYS`.

**The rules live in the prompts.** The frame carries only the one every message shares (one genuine thing from the past with this person, or nothing), so a step that needs a link says so, and cold's word ceiling sits in cold's own prompt. There is no validation of content after the fact and no retry; a draft is sent as written.

## The history

The model is handed the history as the conversation itself: Pydantic AI's `message_history` (`core/agents/history.py`), each email we sent one of its own turns and each email they sent one of theirs, oldest first and verbatim, with no cap. A step is written after every turn in every thread of every deal the lead is in; a conversation's turn reads the turns of its own deal's thread. The prompt for the email at hand comes after it:

```
user        Every email with them so far, oldest first.
assistant   the E0 email, as sent
user        [no reply for 3 business days]
assistant   the E1 email, as sent
user        their reply, quoted history stripped
user        the prompt for this message
```

**What was said, never what we were told.** Each send records its rendered prompt on the `Message` (`prompt`, `prompt_path`, `step`), for whoever reads the mail log, but the model never reads it again: an old step's instructions would sit beside today's and disagree with them. Bounces and out-of-office mail are facts about the thread, not turns, and never reach the model. A session deciding in the model's place has no message history, so its brief carries the same history written out as text (`transcript`).

## What each call returns

**A step**: `write_email` makes one structured call returning an `Email` (`subject`, `message`). The email that opens a thread must carry a subject; a later one goes under the thread's.

**A turn**: a `DealTurn`, logged whole with its move.

| `to` | Carries | Effect |
|---|---|---|
| `conversing` | `message` | The message is sent in the thread (`References` = every Message-ID in it, `In-Reply-To` = the last), and the turn is logged as a move from `conversing` to itself. |
| `completed` | `outcome` | Nothing is sent. The deal is closed with that outcome. `unsubscribed` also puts the address on the account-wide list, which ends the lead's other open deals. |

A session can take a turn in the model's place, with no model key: `outsend send --agent-draft` hands the reply over, and `agfsm fire deal-<sequence>:<id> --agent` then `--answer` does the same for one deal. Its message is sent the same way, and the move is logged as decided by `agent`.

## What the model knows about the lead

`leads/summaries.py:materialize_profile_summary_if_missing(lead)` extracts a fact list from the lead's stored `profile_text` once, before the first prompt step. The frame shows those facts, or the raw text if the extraction has not run. The history replaces any summary of the conversation itself.

See [Template Variables](./template-variables.md).
