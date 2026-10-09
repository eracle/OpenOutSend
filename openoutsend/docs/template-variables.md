# Prompt Context Reference

This describes what the outreach agent's prompt receives. There is one prompt, `core/templates/prompts/outreach_agent.j2`, branching on `is_first_touch`; the shared half of its context is assembled in **`core/agents/prompt.py`** (`base_context`, `_format_facts`) and the conversation half in `core/agents/outreach.py` (`_render_system_prompt`).

There is **no Voyager profile dict** — the browser/scraping channel was removed. Lead context comes from the licensed Lead Finder payload that was stored at discovery, not from a live fetch.

## What the prompts receive

- **Campaign context** — `product_docs`, `campaign_target`, and `booking_link` from the `Campaign`.
- **Seller identity** — the operator's name (`core/operator.py:seller_name()`, read from the Django `User` / `SiteConfig`, not scraped), used to keep the LLM from misattributing greetings in a reply.
- **Lead facts** — the deal's `profile_summary`: a mem0-style JSON fact list materialized once from the lead's **stored `profile_text`** (headline, company description, title, seniority, industry, location). No positions/education/URNs — those came from the retired scrape and no longer exist.
- **Conversation facts** (in thread only) — the deal's `chat_summary` (running fact list folded from IMAP-read replies) plus a recency window of verbatim turns from the mail log, `today`, and the unanswered-outgoing counters. Omitted entirely on a first touch.

## Output

`OutreachDecision{action, subject?, message?, outcome?}` at both ends of the thread. `subject` is required on a first touch and omitted in thread; `outcome` is required with `mark_completed`. There is no `follow_up_hours` — nothing is scheduled, so the agent is never asked when to speak next.

To see the exact fields passed to the template, read `core/agents/prompt.py:base_context` and `core/agents/outreach.py:_render_system_prompt`. The fact-list shapes are produced by `core/db/summaries.py`.
