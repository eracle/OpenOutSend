# The Send Path Has No Feedback From Delivery Failure

> ## 📥 Inherited from OpenOutreach, 2026-08-19. Open, unbuilt, and now **the most valuable card here.**
>
> `delivery_policy.py`, `warmth.py` and the mail log all live in `cold_outreach/` on this side, so
> this is our problem now.
>
> **Two reasons it matters more here than it did there.** A bounce is the only verdict on whether an
> address was real, and OpenOutreach can no longer see one — the boundary is one-way, so it is
> emitting addresses with no idea which are dead. It named that as the single cost of the split with
> **no substitute**. If this side ever measures a bounce rate, that number is the one thing worth
> sending back, as a single event, and `lead_id` rides in the export as the join key so it stays
> cheap to do.
>
> Second: `warmth.py` already halves a box's capacity when its bounce rate exceeds tolerance, so
> half the mechanism exists. What this card describes is the other half.

> **Mechanism superseded 2026-08-13 by [[2026-08-13-p1-e2-mail-log-epic]]**, which
> **shipped that day**: a delivery failure is now a recorded fact (`DeliveryEvent`,
> against the send it killed), a non-delivery report is kept out of the
> conversation by construction, and a box bouncing above 5% halves its own
> capacity. What a bounce should *mean for a deal* is still entirely open, and is
> this card. **This card keeps its policy
> questions** — what entity "bounced" is a property of, whether a bounced address
> ends the pursuit or returns it for re-enrichment, and whether the system may
> halt itself — none of which the epic decides. It also keeps the measured
> evidence, which is not reproducible.

- **Status:** Done — **a bounce naming a dead address now ends the pursuit.** The address
  joins the suppression list, its open deals reach `UNDELIVERABLE`, and a refusal at the
  SMTP door does the same thing through the same function. The policy is in *What the
  policy turned out to be* below, and every open question this card raised is answered
  there. **The one criterion it could not close — "is my domain listed?" — moved to
  [`p1-e2-sending-domain-reputation-check`](p1-e2-sending-domain-reputation-check.md)**,
  which is a reputation read rather than a bounce mechanism and had no business staying
  here.
- **Priority:** Medium — was Critical while dead addresses were mailed forever; the
  failure mode that earned that rating is closed.
- **Effort:** Small
- **Area:** Pipeline

> **This card states a problem, not a solution.** It is written for someone
> coming to it fresh, from first principles. The evidence below is all measured
> on production; nothing here prescribes a design, a schema, or a state machine,
> and the omission is deliberate.

## User Story

As an OpenOutreach operator, I want the pipeline to stop mailing addresses that
do not exist, and to notice when it is doing so — so that a sending domain is not
destroyed silently, over months, by a failure mode nothing in the system can
currently perceive.

## What happened

`indieoutreach.app` was provisioned 2026-06-10 and sent from `ercole` until
2026-08-06. On 2026-08-06 it was found **listed on SURBL's abuse list** (verified
independently by DNS: `indieoutreach.app.multi.surbl.org → 127.0.0.64`) and
reported listed on **Spamhaus DBL** by MXToolbox. Domain authentication is clean
and was never the issue: SPF, DKIM (2048-bit), DMARC at `p=quarantine`, Google
Workspace MX.

Measured on the two live instances at the moment sending was halted:

| | ercole | ylenia |
|---|---|---|
| deals emailed | 408 | 33 |
| outgoing messages | 682 | 45 |
| inbound messages | 3 | 1 |
| of those, genuine human replies | **0** | **0** |
| `SendVerdict` rows (all time) | **0** | **0** |
| `Mailbox.daily_limit` when halted | **90** | 5 |

All three of ercole's inbound messages are Office 365 non-delivery reports, not
replies:

```
msg  84  deal 95   2026-07-22  jzhang wasn't found at opsmateai.com
msg 168  deal 95   2026-07-25  jzhang wasn't found at opsmateai.com   ← re-mailed, bounced again
msg 678  deal 761  2026-08-02  sujitharramreddy wasn't found at aizencorp.com
```

Zero human replies from 408 deals is not merely a null result. If the true reply
rate were 1%, the probability of observing zero is ~1.7%; at 0.5%, ~13%. Whatever
the real rate is, it is well under 1% — but **nothing in the data distinguishes
"the copy is wrong" from "the mail never reached an inbox,"** which is itself
part of this problem.

### Update 2026-08-12 — the zero was partly an artefact

Re-measured on `ercole` six days later (590 deals emailed, 91 MB CRM,
`pragma integrity_check ok`). Two things changed the picture above:

**There was a human reply, and a second bug ate it.** `hans@basicops.com` answered
deal 829 with *"No thanks"* on 2026-08-02. It is not in the CRM and never was —
lost by an unrelated IMAP cursor regression, documented in
[[p1-e2-inbound-mail-silent-skip]]. So the true count is **1 reply from 590
deals**, not 0, and the reply that did arrive was a decline. The statistical
argument above still holds directionally, but "zero" was measuring two failures at
once and should not be quoted as a pure deliverability signal.

**The deal-95 loop recurred, unchanged.** On 2026-08-12, deal 1166:

```
07:02:05 OUT  Hey Nate, I came across your work at Vital…
07:02:06 IN   ** Address not found ** … nate@vitalroutines.ai
07:09:44 OUT  Got a bounce-back on that address. Is nate@vitalroutines.ai
              still the right one, or should I try something else?
07:09:45 IN   ** Address not found ** … nate@vitalroutines.ai
```

Same shape as deal 95 in July: the agent read the NDR as the prospect's reply and
apologised to the dead address, bouncing again. It terminated only because the
deal closed — and closed as `wrong_fit`, so the qualifier's outcome data now
records a dead address as a bad-fit human.

**`SendVerdict` is still `0` rows** against 590 sends. Point 2 below is confirmed
unchanged: the gate has never fired, because `record_failure` runs only from
`sender._deliver`'s exception path and a hard bounce is not an exception.

Every NDR now in the box is trivially separable from a real reply — all seven
carry `Return-Path: <>` and `Auto-Submitted: auto-replied`, and six of seven are
`Content-Type: multipart/report`. Hans's genuine reply carries none of the three.
This is stated as evidence that the discrimination is cheap, **not** as a proposed
design — the modelling questions below are untouched by it.

One sequencing constraint this adds: recovering the lost mail in
[[p1-e2-inbound-mail-silent-skip]] re-reads a stored NDR, so classification
has to exist before that cursor is rewound.

## The failure, as observed

**1. A non-delivery report is indistinguishable from a lead reply.**
`_upsert_reply()` (`openoutreach/emails/inbox.py`) skips only our own outbound
copies; any other `From` becomes an incoming `ChatMessage`. An NDR threads
normally (it quotes our headers) and comes from the receiver's postmaster, so it
passes both checks. The deal then reads as *engaged*. Live consequence on deal 95:

```
[INF] chat facts for https://www.linkedin.com/in/jameszhangboston:
  • The lead attempted to send a message to jzhang@opsmateai.com.
  • The email address jzhang@opsmateai.com does not exist at the opsmateai.com domain.
  • The lead uses Office 365 for email.
[INF] follow_up reply: Just realized my first email bounced — jzhang@opsmateai.com
      isn't the right address. My bad. Do you have a different email I should reach you at?
[INF] email sent from eracle@indieoutreach.app to jzhang@opsmateai.com
```

The summariser attributed *our* send and *their* server's banner to the lead. The
agent understood the address was dead and mailed the apology to the dead address —
producing the second NDR three days later.

**2. The capacity ramp cannot see asynchronous failure, so it accelerates into it.**
`emails/warmth.py` grows a box's daily limit off its own Sent history, gated on
`_receiver_pushed_back()`, which reads `SendVerdict` rows. Those are written only
from `sender._deliver`'s exception path. A hard bounce is not an exception: the
receiver accepts with `250 OK` and reports the failure later, as a separate
message. So the gate has returned "clean" on every pass since June, capacity
compounded ×1.5/day from a floor of 5 toward `WARM_CEILING_SENDS` (~261), and the
box reached 90/day while bouncing. **The one signal the design depends on is
structurally unable to fire for the dominant failure mode.**

**3. Nothing observes what the outside world thinks of the domain.** No component
reads a blocklist, a placement signal, or a complaint rate. The listing was found
by a human running MXToolbox two months in. Google Postmaster Tools is not an
option at this volume — it needs ~100–200 messages/day *to Gmail* before it
classifies anything, and the v1 API was retired 2025-09-30.

**4. The bounce rate is unmeasurable, so severity is unknown.** Only NDRs that
thread onto an existing deal become visible at all; any that don't are unread in
the box. Two confirmed hard bounces against 408 deals is a floor, not a rate.

## Domain facts any solution has to live with

Stated as constraints, not as hints toward a design:

- **A hard bounce is a fact about an address, not about a person.** The same
  human may be reachable at a different address tomorrow — enrichment resolving a
  replacement is a normal event, not an edge case. Whatever suppression is built,
  it must not foreclose that. (This is why the opt-out mechanism,
  `Lead.disqualified`, is the wrong instrument: it binds to the person and is
  cross-campaign.)
- **`Lead.email` is a plain `CharField`.** Addresses are not entities today; they
  have no identity, no history, and no state. A lead has exactly one, overwritten
  in place.
- **`sender.suppressed()` declines a send but does not terminate the deal.** Both
  call sites log and `return` with the state unchanged. This is safe only because
  the existing opt-out path always closes the deals at the same moment it
  suppresses. A suppression mechanism that does not close deals leaves them to be
  re-selected every reconcile — re-running the outreach agent (an LLM call) each
  time, forever.
- **`Deal` is `unique(lead, campaign)`.** The same lead — and the same address —
  can appear in a second campaign after the first one ended, so anything recorded
  per-deal does not carry across.
- **Repeated sending to nonexistent mailboxes is a leading cause of exactly the
  blocklisting that occurred.** It is not only wasted spend; it is plausibly the
  proximate cause of the listing.
- **The daemon is self-hosted and has no web surface.** Any external signal has to
  be reachable from the daemon (DNS, IMAP, an API call) without a callback.

## Why this matters beyond one box

This is not an `ercole` operations incident. Every OpenOutreach install shares
this send path, so any operator running it will reach the same failure by the same
route: send, bounce invisibly, ramp, get listed, discover it by hand months later.
A tool that silently destroys the user's sending domain is worse than one that
sends nothing. The same ingest path also feeds
[[p2-e3-inbound-agentic-email]], where the agent answers replies unsupervised —
an NDR misread as a lead reply is considerably worse there.

## Open questions

Deliberately unanswered. These are the ones that seem load-bearing; a good
solution may reject the framing entirely.

- **What entity is "bounced" a property of?** The lead is wrong (the person is
  fine). The deal is questionable (a deal is a pursuit within a campaign; the
  bounce outlives it and crosses campaigns). The address is the obvious candidate —
  but addresses are not modelled as anything today. Note the naming hazard:
  `Mailbox` in this codebase already means *our sending box*, not the recipient's.
- **Is a delivery failure that arrives as mail the same kind of event as one that
  arrives as an SMTP response?** They carry the same enhanced status and mean the
  same thing to a receiver. They arrive down completely different paths.
- **Should a bounced address end the pursuit, or send it back for re-enrichment?**
  Re-enrichment costs a paid credit and may well return the same dead address.
  Terminating forecloses the replacement-address case the constraints require
  keeping open.
- **What is the feedback signal for volume, if bounces are one input among
  several?** Postmaster is unavailable at this volume; DNSBLs, IMAP-visible NDRs,
  and seed-list placement tests are the candidates that work at any volume.
- **Should the system be able to halt itself?** Nothing today can stop sending on
  its own recognisance, whatever it observes.

## Done when

Stated as outcomes; how they are achieved is open.

- [x] A non-delivery report never appears in a deal's conversation or chat facts,
      and never influences the agent's reasoning.
- [x] No message is ever sent to an address already known to be undeliverable —
      from any campaign, at any later date. *(A dead address goes on the same
      `Suppression` list an opt-out uses, so ingest parks new rows for it and
      `sender.suppressed` re-asks after the agent has written.)*
- [x] A deal that cannot be delivered to reaches a terminal, rather than being
      re-selected indefinitely. *(`DealState.UNDELIVERABLE` — its own state, not
      `UNSUBSCRIBED`, because nobody asked for anything. Both pools name the state
      they want, so a terminal state is unsendable without an exclusion to forget.)*
- [x] The send volume of a box that is bouncing goes **down** without a human
      intervening. *(Already true — `warmth.py` halves capacity above tolerance; a
      suppressing bounce still records its `DeliveryEvent`, so this keeps working
      for the statuses that now also end the pursuit.)*
- [→] An operator can answer "what is my bounce rate?" and "is my domain listed?"
      from the system, not from a third-party website. **Moved to
      [`p1-e2-sending-domain-reputation-check`](p1-e2-sending-domain-reputation-check.md)**
      — it is a reputation read, not a bounce mechanism.
      *(Superseded text: the rate is arithmetic the
      log can already answer; **DNSBL listing is not built**, and it is the honest
      remainder of this card.)*
- [x] A replacement address found later for the same person is still sendable.
      *(Suppression is keyed on the address, never on the lead or the deal.)*
- [x] Each behaviour is covered by a test.

## What the policy turned out to be

**Only the receiver's explicit statement that there is nobody there.** The statuses
are listed one by one in `emails/delivery_policy._DEAD_ADDRESS_STATUSES` — 5.1.1,
5.1.2, 5.1.3, 5.1.6, 5.1.10, 5.2.1 — rather than matched as a class, because the
list *is* the policy and has to be auditable without knowing RFC 3463 by heart.

**Two exclusions carry most of the safety.** `5.7.x` is the policy/reputation class,
a statement about this sending box rather than the recipient; suppressing on it would
delete good prospects from every future campaign for as long as our standing was
poor, turning a recoverable dip into permanent, invisible list attrition. It already
has a home in `Response.BLOCKED`, which pauses the box and calls for an operator.
`5.2.2` (mailbox full) is a deferral wearing a 5.x code — the mailbox empties and the
person is real. A report carrying **no** parseable status suppresses nothing: an
unreadable report is not evidence.

**The open questions resolved as follows.** *What entity is "bounced" a property of?*
The **address** — `Suppression` was already address-keyed for the opt-out duty, and
its `reason` field already anticipated a bounce. *Is a mail-borne failure the same
event as an SMTP response?* **Yes** — same enhanced status, same meaning — so both
paths call one `delivery_policy.stop_mailing` and cannot drift; the only difference
is which door the answer came home through. *End the pursuit or re-enrich?* **End
it**: re-enrichment spends a paid credit and may well return the same dead address,
and ending it forecloses nothing, since a replacement address is sendable the moment
it arrives. *Should the system halt itself?* Untouched — still `warmth`'s question.

## Not this card

- Pre-send address verification at find-email time (a different problem: stopping
  bad addresses entering the pipeline, rather than reacting once they have).
- Delisting `indieoutreach.app` and the operational recovery of that domain.
- Falling back to another channel to ask for a valid address.

---

*An implementation was attempted on 2026-08-06 and reverted unmerged — it is
recoverable at `51ba8d6` in the OpenOutreach reflog. It is recorded here only so
the work is not repeated by accident; it was reverted because its modelling was
not convincing, and it should not be treated as a starting point.*
