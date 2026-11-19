# Prompt Context Reference

This describes what the outreach agent is given. Every call has two parts: the **frame**, `core/templates/prompts/frame.j2`, as the system prompt, with its variables assembled in **`core/agents/prompt.py`** (`base_context`); then the **history** with the lead as the call's message history, and last the **user message**, the prompt for this email (`core/agents/outreach.py`, `write_email`).

There is **no Voyager profile dict**: the browser/scraping channel was removed. Lead context comes from the licensed Lead Finder payload that was stored at discovery, not from a live fetch.

## What the frame receives

- **Campaign context**: `product_docs`, `campaign_target` and `booking_link`, from the install's config (`OUTSEND_PRODUCT_DOCS`, `OUTSEND_CAMPAIGN_TARGET`, `OUTSEND_BOOKING_LINK`).
- **Seller identity**: `self_name`, the operator's name (`core/operator.py:seller_full_name()`), used to keep the LLM from misattributing greetings in a reply.
- **Lead facts**: `profile_summary`, the fact list extracted once from the lead's **stored `profile_text`** (headline, company description, title, seniority, industry, location), or that text itself if the extraction has not run.
- **`today`**, the date the email is written.

The frame carries one rule, the one every message shares: name one genuine thing from the past with this person (what they wrote or did, their product, their market), sourced from the lead facts or the history, or nothing at all. Every other rule comes with each prompt.

## What a step file receives

A step file (`core/sequences.py`) is rendered before the model reads it, with the fields `emails/steps/send.step_fields` builds: the lead's fields, every other key the piped line carried for this sequence (`Deal.fields`, any JSON value: a link, a list of leads), and `prompt_line`, the drawn move's text. A `{{ field }}` the deal does not carry is an error for that deal, never an empty string.

## The history

`core/agents/history.py:history(lead, now)`: every turn in every thread of every deal the lead is in, oldest first and verbatim, as Pydantic AI messages. Each of our sends is a turn of the model's own, the email as sent; each of theirs is their words; a `[no reply for N business days]` line follows each of our sends that went unanswered. The prompts our emails were written from are never in it. Empty on a first touch, so an opener is written from its prompt alone.

## Output

A step returns an `Email{subject?, message}` (`core/agents/outreach.py`): `subject` is required on an opener and omitted in thread. A conversation's turn returns a `DealTurn{to, message?, outcome?}` (`leads/conversing.py`): `message` when it answers and stays in `conversing`, `outcome` when it ends the deal. Neither says when to speak next: nothing is scheduled by the model.
