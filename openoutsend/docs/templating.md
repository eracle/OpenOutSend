# Outreach Messaging

Outreach messages are written by an LLM, not filled into string templates. There is no per-account message template: the agent writes each email from the install's campaign context, the lead's stored profile, the whole history with that lead, and the prompt for the email at hand.

One agent (`core/agents/outreach.py`) writes every message, and it returns an `OutreachDecision{action, subject?, message?, outcome?}`. What differs between messages is the prompt that comes last:

- **First touch**: the first step of the deal's sequence. Cold's is `sequences/cold/E0-opener.md`, a Markdown file rendered by Jinja with `StrictUndefined`; it draws a move from the prompt-line pool with `{{ prompt_line }}`. The decision must be `send_message` with a `subject`. An operator's copy at `~/.openoutsend/sequences/cold/` replaces the shipped folder whole.
- **Later step**: each step file after the first, due its days after the previous step's send (`planner.steps_due`). Written in the same thread with no new subject, and stopped by a reply or a piped `stop`.
- **Follow-up**: `FOLLOW_UP_PROMPT` in `emails/steps/follow_up.py`, switched by `OUTSEND_FOLLOW_UPS`: `send_message` in the same thread, or `mark_completed` on a plain wrong fit.
- **In thread**: on deals with an unanswered reply, `REPLY_PROMPT` in `emails/steps/reply.py`: `send_message` / `mark_completed` / `suppress`. See [Outreach Agent](./outreach_agent.md).

The agent never decides *when* to speak, only what to say. A deal becomes actionable because a reply arrived, a step's days have passed or a follow-up gap has elapsed, all derived from `start_at` and the mail log in `planner.py`, never from a countdown the agent re-arms. There is no `wait` action and no interval to choose.

The frame lives in `core/templates/prompts/frame.j2` and its variables are assembled in `core/agents/prompt.py`. See [Template Variables](./template-variables.md) for what it receives.
