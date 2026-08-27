# Prompt lines

A **prompt line** is a piece of prompt describing the move the first email makes. It
drops into the opener's system prompt; everything else about the message — who you are,
the product, the Mom Test discipline, the hard rules — is the same whichever line is
chosen.

**Prompt lines are for the first touch only.** A reply answers what the person actually
wrote, which is not a move anybody picks in advance.

## Writing one

Three keys, in TOML:

```toml
id = "peer-curiosity"
when = "One line: which leads this suits."
prompt = """
The move, addressed to the model, in the second person.
"""
```

Drop the file here and it ships. Drop it in `~/.openoutsend/prompt_lines/` and it is
yours alone — an id in both places resolves to yours, so a shipped line can be
overridden without editing an installed package and without losing the edit on upgrade.

## What belongs in one, and what never does

**In:** the move, why it works, the shape of the ask, which leads it suits.

**Out:** anything the generator already enforces — the recipient's language, the word
ceiling, no links, no meeting request, no signature, sourced claims only. Those live in
`core/agents/outreach.py` precisely so that a dozen files do not have to remember them,
and so no line can drop one by being edited carelessly.

**Never a mail-merge template.** `Hi {{first_name}}, I noticed {{company}}…` is what
Instantly already does, and doing it here throws away the reason there is a model in the
loop at all. Describe the move; let the model write the sentence.

**Never a claim the record cannot source.** *"I've met people from your company"* works
when a human says it because it is true. A prompt line instructing the model to say it
manufactures a small lie for a thousand strangers, and it is the one failure here that
cannot be taken back.

## How one gets picked

At random per send, unless `outsend send --prompt-line <id>` names one. **The choice is
recorded on the message** (`prompt_line_id`, plus a hash of the text so an edited line is
not silently pooled with its older self), which is what makes comparing them later a
question of reading the log rather than of having planned an experiment.

There is no scoring yet, and none is faked. Randomly chosen and honestly recorded is the
whole of it — see `roadmap/p1-e2-sender-message-generation.md` for the learner that reads
this log once there is enough of it.
