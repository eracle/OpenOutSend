# Prompt Context Reference

This describes what the outreach agent is given. Every call has two parts: the **frame**, `core/templates/prompts/frame.j2`, as the system prompt, with its variables assembled in **`core/agents/prompt.py`** (`base_context`); and the **user message**, built in `core/agents/outreach.py` (`conversation`): the history with the lead, then the prompt for this email.

There is **no Voyager profile dict**: the browser/scraping channel was removed. Lead context comes from the licensed Lead Finder payload that was stored at discovery, not from a live fetch.

## What the frame receives

- **Campaign context**: `product_docs`, `campaign_target` and `booking_link`, from the install's config (`OUTSEND_PRODUCT_DOCS`, `OUTSEND_CAMPAIGN_TARGET`, `OUTSEND_BOOKING_LINK`).
- **Seller identity**: `self_name`, the operator's name (`core/operator.py:seller_full_name()`), used to keep the LLM from misattributing greetings in a reply.
- **Lead facts**: `profile_summary`, the fact list extracted once from the lead's **stored `profile_text`** (headline, company description, title, seniority, industry, location), or that text itself if the extraction has not run.
- **`today`**, the date the email is written.

The frame carries no rules. Those come with each prompt.

## What a step file receives

A step file (`core/sequences.py`) is rendered before the model reads it, with the fields `emails/steps/send.step_fields` builds: the lead's fields, every other key the piped line carried for this sequence (`Deal.fields`, any JSON value: a link, a list of leads), and `prompt_line`, the drawn move's text. A `{{ field }}` the deal does not carry is an error for that deal, never an empty string.

A single-brace `{field}` is not Jinja, so it reaches the model as written. It is what the model **writes**: a prompt asking for the link "written exactly as `{order_url}`" gets the placeholder back, and `emails/steps/drafts.py` fills it from the same fields (`emails/steps/send.piped_fields`, everything above except `prompt_line`) once the model has written. A placeholder naming a field the deal lacks is redrafted once, then held.

## The history

`core/agents/history.py:history(lead, now)`: every turn in every thread of every deal the lead is in, oldest first and verbatim. Each of our sends shows the prompt it was written from and the email as sent, each of theirs shows their words, and a `[NO REPLY for N business days]` marker follows each of our sends that went unanswered. Empty on a first touch, so an opener is written from its prompt alone.

## Output

`OutreachDecision{action, subject?, message?, outcome?}` for every message. `subject` is required on an opener and omitted in thread; `outcome` is required with `mark_completed`. There is no `follow_up_hours`: nothing is scheduled, so the agent is never asked when to speak next.
