# Follow-up Agent: Identity Mismatch, Missing Backoff, No Sentiment Exit

> ## 📥 Inherited from OpenOutreach, 2026-08-19. Three defects in the agent that came across with it.
>
> The outreach agent is `cold_outreach/core/agents/outreach.py` on this side, with its prompt at
> `cold_outreach/core/templates/prompts/outreach_agent.j2`. Every defect this card names travelled
> with the code.
>
> ⚠️ **Bug 2 is closed, and not the way this card imagined.** OpenOutreach had removed chasing
> entirely before the port, which deleted the very thing a backoff would schedule; chasing is now
> back (`emails/steps/follow_up.py`), but **the agent no longer owns the clock at all**. The gaps are
> constants in `core/conf.py`, and who is due is derived from the mail log by
> `leads/pools.awaiting_follow_up`. There is no `wait` verdict to fail to push out, no
> `next_follow_up_at` to re-arm, and therefore nothing that can re-read an unchanged context five
> times in a window. A backoff was the wrong shape for the problem: the defect was that a decision
> about *timing* was being made by the thing that writes the *message*.

- **Status:** To Do — **bug 2 is closed** (see above). Bugs 1 and 3 stand.
- **Priority:** Medium
- **Effort:** Small — what is left is the identity mismatch and the sentiment exit.
- **Area:** Pipeline

> Evidence: `roadmap/bug.logs` (a worker-loop capture). Three distinct defects surface on the deal `paolo-tardani-336b3359`.
>
> **Sequenced after the email-first pivot (not a prerequisite).** Decision 2026-06-07:
> ship the email epic ([[p1-e3-email-first-pivot-epic]]) first and fix these bugs afterward,
> rather than gating the pivot on them. The same follow-up agent is reused for outbound email
> ([[p1-e3-email-agentic-outreach]]) and for unsupervised auto-replies
> ([[p2-e3-inbound-agentic-email]]), so until this lands the identity-mismatch risk (bug 1)
> applies to outbound email too — accepted as known debt for Layer 1. This work still gates
> Layer 2 ([[p2-e3-inbound-agentic-email]]), where the agent answers replies unsupervised.

## User Story

As an OpenOutreach operator, I want the `follow_up` agent to message the correct identity, back off after a `wait` verdict, and disqualify leads who signal frustration — so the pipeline stops re-spending LLM calls on an unchanged context and stops personalizing outreach against the wrong person.

## The three bugs

### 1. Identity mismatch: deal slug ≠ actual lead (highest risk)

The deal is keyed `paolo-tardani-336b3359` ("Paolo Tardani"), but every `chat facts` block for it reads:

```
• The lead's name is Diego.
```

The deal record and the person actually in the conversation disagree. Outreach is being personalized against the wrong identity. Root cause is upstream of `follow_up` — either profile misattribution at discovery, a conversation threaded onto the wrong deal, or a stale slug. Needs tracing through how a deal slug gets bound to a conversation on the professional network.

### 2. No backoff after a `wait` verdict

`follow_up paolo-tardani-336b3359` fires **five times** in a tight window (log lines 13, 29, 46, 61, 74). Every run returns the same verdict:

```
follow_up agent for paolo-tardani-336b3359: wait
```

The chat-facts payload is byte-for-byte identical each pass, and some slots are `0h00m` apart (line 28). A `wait` verdict does not push the deal's next-action time out, so it re-enters adjacent slots and re-runs the LLM against an unchanged context. A `wait` should reschedule far out (hours/days), not allow near-immediate re-selection.

### 3. No sentiment / disqualify exit

The facts capture a clear negative signal:

```
• Diego expressed that he does not understand why the other person is asking him all these questions.
• Diego responded with politeness but some frustration or confusion about the line of questioning.
```

The agent correctly chose `wait` rather than pushing more questions — but there is no transition that flags the deal as souring or disqualifies it. It just sits in the queue getting re-polled (compounding bug 2). The interrogation-style questioning that frustrated the lead is also an upstream prompt-quality smell.

## Why

Together these waste LLM spend (bug 2), risk reputational damage by messaging the wrong identity (bug 1), and keep dead leads alive in the queue (bug 3). Bug 2 is the cheapest fix and stops the bleeding; bug 1 is the most serious; bug 3 prevents the queue from filling with un-actionable deals.

## Tasks

### OpenOutreach (main project)

- [ ] **Backoff:** when the `follow_up` agent returns `wait`, set the deal's next-action time forward by a sensible interval (config-driven) so it cannot be re-selected in adjacent slots. Add a test asserting a `wait` verdict pushes `next_action_at` out.
- [ ] **Identity:** trace where `paolo-tardani-336b3359` got bound to Diego's conversation. Add a guard/assertion that the deal's stored name matches the conversation participant, and log a warning (or quarantine the deal) on mismatch.
- [ ] **Sentiment exit:** add a state transition that flags a deal as souring / disqualified when the chat facts carry a frustration/confusion signal, removing it from the active follow-up rotation.
- [ ] Review the follow-up question prompt for the interrogation pattern that frustrated this lead.

## Out of scope

- Rewriting the discovery/profile-scrape pipeline wholesale. Only the slug↔conversation binding guard is in scope here.
- Marketing/upsell log lines interleaved into the `[INF]` stream and wrapping mid-URL (`roadmap/bug.logs` lines 9, 60, 97) — cosmetic, log-parser hygiene only; not part of this story.

## Done when

- A `wait` verdict reschedules the deal so it is not re-run against an unchanged context within the same short window.
- A deal whose stored identity disagrees with the conversation participant is flagged rather than silently messaged.
- A lead showing frustration is moved out of the active follow-up rotation.
- Each behaviour is covered by a test in the OpenOutreach submodule.
