# after-a-demo

Three emails after a product demo: a thank-you with one question, an honest answer to what they
asked, and a real last note. Copy the folder, rewrite the prompts for your product, and keep it in
your own git.

```
E0-1d-thanks-and-one-question.prompt.j2     the business day after the demo
E1-3d-the-thing-they-asked-about.prompt.j2  3 business days after E0
E2-5d-last-note.prompt.j2                   5 business days after E1
leads.jsonl                                 one lead, as your export would print it
```

- **`{{ field }}` is filled in from the piped line** before the prompt reaches the model:
  `{{ demo_notes }}`, `{{ open_question }}`, `{{ booking_url }}`. A field the line lacks fails that
  lead.
- **`start_at` is the demo.** `E0` counts its one business day from it. Set it to the demo's real
  date before you pipe the line: a `start_at` already past `E0`'s grace skips that step.
- **`"stop": true` ends it.** Re-pipe the line when the call is booked.

## The whole loop

An install mailing a sequence like this one usually has follow-ups off, because its steps already
say everything:

```bash
export OUTSEND_FOLLOW_UPS=off

outsend --sequence examples/sequences/after-a-demo < examples/sequences/after-a-demo/leads.jsonl
outsend send --sequence examples/sequences/after-a-demo --agent-draft   # read the first draft
outsend send --sequence examples/sequences/after-a-demo                 # send what is due
sed 's/"stop": false/"stop": true/' examples/sequences/after-a-demo/leads.jsonl \
  | outsend --sequence examples/sequences/after-a-demo                  # the call is booked: stop
```

`--agent-draft` hands the opener back to the calling agent as `draft_pending` instead of writing it
with `AI_MODEL`; answer it with `--subject` and `--body`, or drop the flag to let the model write.
