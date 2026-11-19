# Outreach Messaging

Outreach messages are written by an LLM, not filled into string templates. There is no per-account message template: the agent writes each email from the install's campaign context, the lead's stored profile, the whole history with that lead, and the prompt for the email at hand.

A step's email is written by one call (`core/agents/outreach.py`, `write_email`) that returns an `Email{subject?, message}`; a conversation's turn (`leads/conversing.py`) returns a `DealTurn`. What differs between messages is the prompt that comes last:

- **First touch**: the first step of the deal's sequence. Cold's is `sequences/cold/E0-opener.prompt.j2`, a prompt rendered by Jinja with `StrictUndefined`; it draws a move from the prompt-line pool with `{{ prompt_line }}`. The email must carry a `subject`. An operator's copy at `~/.openoutsend/sequences/cold/` replaces the shipped folder whole.
- **Later step**: each step file after the first, due its days after the previous step's send (the wait of its `awaiting_E<k>` state, `leads/lifecycle.py`). Written in the same thread with no new subject. A reply does not stop it; a piped `stop` does.
- **In the conversation**: in an install that holds conversations (`OUTSEND_CONVERSATIONS=on`), a deal whose every step has gone and whose lead wrote back is answered from the folder's `conversation.prompt.j2`, or the built-in prompt in `leads/conversing.py`: the model answers in the thread, or ends the deal with an outcome. See [Outreach Agent](./outreach_agent.md).

The agent never decides *when* to speak, only what to say. A deal becomes actionable because a reply arrived or a step's days have passed, read from the deal's state and the time of its last move, never from a countdown the agent re-arms. There is no `wait` action and no interval to choose.

The frame lives in `core/templates/prompts/frame.j2` and its variables are assembled in `core/agents/prompt.py`. See [Template Variables](./template-variables.md) for what it receives.
