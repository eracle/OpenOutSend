# Ask the operator for the daily send cap during onboarding

> ## 📥 Inherited from OpenOutreach, 2026-08-19. Re-read it before building — its premise moved.
>
> The card asks onboarding for a daily send cap. **Since it was written, the cap stopped being
> configured at all**: `cold_outreach/emails/warmth.py` *measures* it from the box's own Sent folder
> (p75 of days it actually sent, stepped ×1.25 above yesterday's allowance when the receiver hasn't
> pushed back), because a number a human types is wrong in one of two directions — throttling a box
> that has carried more for months, or handing a fresh box a seasoned one's volume.
>
> So the useful question here is no longer "ask for the cap" but **"should an operator be able to
> override the measurement downward, and how is that not a footgun upward?"** That is a smaller card
> than this one, and worth rewriting rather than implementing as filed.

- **Status:** To Do
- **Priority:** Low
- **Effort:** Low
- **Area:** Onboarding

Split out of [[p2-e2-onboarding-legal-copy-cap-and-integration-scout]] (item 2) on
2026-07-24, where it was postponed. **Raising the default was the actual need and
already shipped** — `DEFAULT_EMAIL_DAILY_LIMIT` went 30 → 40 (`core/conf.py`), the top
of the 2026 safe band for a warmed Google Workspace box, with migration `emails/0003`
carrying existing boxes still on 30 up to 40 and leaving Admin-retuned boxes alone.

What remains is the *convenience*, not the capability: `Mailbox.daily_limit` is already
per-box and editable in the Django Admin, so an operator who wants a different cap has a
route today. This card is only about asking at connect time.

The cap is applied at mailbox creation in `core/onboarding.py`
(`Mailbox.objects.create_verified(..., daily_limit=DEFAULT_EMAIL_DAILY_LIMIT)`).
Prompt the operator during the mailbox step, defaulting to `DEFAULT_EMAIL_DAILY_LIMIT`,
and persist their choice on the `Mailbox`.

**Acceptance criteria**
- The mailbox onboarding step asks for a max emails/day, pre-filled with the
  current default; pressing Enter keeps the default.
- Input is validated (positive integer) and stored as `Mailbox.daily_limit`.
- Existing installs are unaffected — the default stays `DEFAULT_EMAIL_DAILY_LIMIT`.
- Asked once per never-asked box, consistent with the signature-prompt pattern.

**Guardrail to carry into the copy:** 40/day is near the inbox-level ceiling, not a
throttle to open up. Scaling past it means adding boxes; the prompt should not read as
an invitation to type 200. Reputation damage is the asymmetric risk.

**Gate:** revisit when there's evidence operators actually want to set this at connect
time. Absent that, the Admin field is enough — hence Low priority.
