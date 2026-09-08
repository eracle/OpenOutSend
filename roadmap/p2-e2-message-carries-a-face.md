# The Message Carries a Face — An Avatar, a Chat Shape, and Preset Replies to Pick From

> ## ✍️ Written 2026-08-26, from a live complaint: **the reply rate went to the floor.**
>
> Not inherited from the port. The trigger was an observation about LinkedIn — that a message
> arriving in a chat UI gets answered where the same words in an inbox do not — and the question of
> whether an email could borrow that. The objective is one number and nothing else: **more replies.**
>
> **This card is a hypothesis, not a design.** Everything below is separable, and each piece is worth
> testing on its own; the order they are written in is the order of cost.

- **Status:** To Do — and **gated on measurement**, not on effort. See *Nothing here is decidable yet*.
- **Priority:** Medium — the objective is high-value, but the cheaper explanation for a low reply
  rate is already on the table and untested ([`p1-e2-sender-message-generation`](p1-e2-sender-message-generation.md)).
- **Effort:** Medium for the first two pieces, High for the hosted thread, Medium for reply options
  (a schema field, a thread template, and a flag — no new infrastructure).
- **Area:** Message presentation — `cold_outreach/emails/sender.py` builds the message today, and it
  builds a plain-text one.

## Where this stands, for whoever picks it up next

**Built:** one prerequisite, and nothing user-facing yet. `cold_message_breach` and its retry loop are
gone from `run_outreach_agent` (`core/agents/outreach.py`) — see *Rules live in the prompt, not in a
retry loop* below for what changed and why. That is the only code change this card has produced so
far; sends still go out exactly as before, plain text, one paragraph.

**Not built:** everything else — the avatar (piece 1), the HTML chat-shaped thread (piece 2), the
`reply_options` field on `OutreachDecision`, the `mailto:`-link rendering, the `core/conf.py` draw-rate
flag, and the LinkedIn-pattern subject line. The two **User Story** sections below describe the target
behavior once those are built, not what `outsend` does today — read them as the spec to implement
against, not as a changelog. The **Done when** checklist is the accurate source of what's actually
finished (checked) versus still open.

## The idea, in four separable pieces

**1. The sender has a face.** Attach the operator's own avatar to the message so the recipient sees a
person rather than an address. Nothing about the lead is involved and **nothing new crosses the
pipe** — this is the operator's picture, configured once, exactly like their signature. The boundary
contract is untouched.

**2. The message has a chat shape.** Short bubbles, a visible thread, the layout of a conversation
rather than of a letter. Borrowed from what a chat UI looks like, not from whose chat UI it is.

**3. There is somewhere to reply that is not the inbox.** A self-hosted page showing the whole
conversation, where the lead can answer without composing an email.

**4. The reply is picked, not composed.** The email itself renders as an HTML thread — the full
history with that lead, in bubbles, oldest to newest — and instead of a blank compose box, the
model's own draft ships with two or three short reply options rendered as `mailto:` links, each one
prefilled with a complete reply body in the recipient's likely voice. Clicking an option opens the
recipient's *own* mail client with that reply already written, one send away — the LinkedIn-DM
one-keystroke feel, from inside their existing inbox rather than a page they have to trust. No
inbound HTTP, no per-lead token, no second store: a `mailto:` link carries `subject` and `body` as
query parameters, built at generation time from text the model already wrote, so it gets the
one-tap feel of piece 3 without piece 3's cost.

## What each one actually costs

| | The cost that decides it |
|---|---|
| **Avatar** | The message stops being plain text. Gmail and Outlook both block remote images by default for an unknown sender, so the face is *absent* in the case that matters most — first contact — and a remote image in a cold email is also the shape of a tracking pixel, which is a spam signal in itself. An **inline attachment** avoids the remote fetch and adds weight and a MIME part. The cheap version of this idea is not an image at all: it is a Gravatar-backed **BIMI/avatar on the sending domain**, which the client renders itself from a source it trusts. There is no operator avatar asset yet, so piece 4 below ships without one — it does not depend on this piece landing first. |
| **Chat shape** | HTML. Every plain-text send today is a deliverability asset — plain text is what a person typing actually produces, and the plays card's own evidence is that visibly-human beats polished. An HTML template is the opposite signal, and multipart doubles the surface a filter reads. |
| **Hosted thread** | A web surface, inbound HTTP, a per-lead identity token, hosting and TLS, and a second store of conversation content with its own erasure duty. `outsend` deliberately has none of that: it is a CLI behind a timer with no web surface, which is what makes it a `pip install`. **This piece does not belong in this repo** — it belongs on the hub, which already has Django, Traefik and a domain, and it overlaps hard with [`p2-e3-inbound-agentic-email`](p2-e3-inbound-agentic-email.md), which is the same web surface filed as a paid tier. |
| **Reply options** | HTML again — the whole thread has to render as a page, not a paragraph, so it inherits the chat-shape cost above and adds to it (a thread of bubbles is a bigger template than one message). `OutreachDecision` (`core/agents/outreach.py`) grows a `reply_options` field the model fills alongside `message`. No web surface and no per-lead token: the `mailto:` link is self-contained, so the "cost" stays confined to this repo — it is a template and a schema change, not new infrastructure. `mailto:`'s own limits are generous for a short reply (Outlook desktop caps the whole encoded URL around 2,000–8,192 characters depending on build, Gmail around 4,096) but not risk-free: some clients mishandle `+` or `%0A` in the body, so a length check alone does not prove the link survives — a spot check across the two or three clients the lead base actually uses is still needed before trusting it. |

## Rules live in the prompt, not in a retry loop — for both fields

**Decided and done — it reverses what had shipped for `message`.** `cold_message_breach` used to check
a cold `message` against the word ceiling, links, em dashes and unfilled placeholders after the model
wrote it, retrying once with the complaint appended before failing the send outright (`COLD_ATTEMPTS =
2`). That machinery was judged more complexity than the problem is worth: `cold_message_breach` and
the retry are gone from `run_outreach_agent`, the same rules already lived in `outreach_agent.j2`'s
"Rules this email cannot break" section and now carry the whole weight alone, and `reply_options` will
join them there rather than getting a check-loop of its own — one place carrying the rules (the prompt
template) instead of two, for both fields alike.

**What that traded away, stated plainly so it isn't rediscovered as a surprise later:** the retry loop
was the only thing standing between "the model ignored an instruction" and a message actually going
out that way. `cold_message_breach`'s own reasoning — "quietly mailing something that breaks the
discipline is the outcome worth avoiding" — and the sibling card's stance that the word ceiling,
no-link and no-em-dash rules are "constraints, not hypotheses… spending statistical budget on them buys
nothing" were both written *because* a prompt instruction alone was judged insufficient enforcement.
This was a bet that a well-worded prompt holds in practice, made without the evidence that would prove
it — real generations should get read by hand (does the model still overrun 75 words, still reach for
a link) once volume resumes, precisely because the safety net is now gone.

## Ships as a cohort split, not a global switch

Piece 4 is the one being built first, and it needs to be judged the same way the rest of this card
already commits to: a presentation change ships as a cohort split against the plain-text send and is
kept or reverted on the reply-rate number, not on how it looks. **Neither an `OUTSEND_*` env var nor a
process-wide toggle is the right shape for that** — an operator does not want to run a whole separate
`outsend` process to A/B one thing, and an env var can only ever turn the HTML thread on for an
operator's *next run*, not hold two cohorts inside the *same* campaign side by side, which is what the
deliverability test actually needs. So it is a **constant in `core/conf.py`** (a draw rate, defaulted
to `0.0`, next to `COLD_WORD_CEILING` and the rest of the send guards — the file that already owns
"the rules everyone reads from one place, not from scattered env vars"), read the same way
[`--prompt-line`](p1-e2-sender-message-generation.md) is: drawn at random per send at that rate unless
pinned per invocation with a CLI flag, and recorded on the row so the comparison is a query rather
than a second log. An operator raises the rate in `conf.py` (or their own override, the same override
path prompt lines already have) to run the test, rather than setting an environment variable. Whatever
the final shape, `0.0` — plain-text, unconditionally — is the shipped default, and the reply-rate split
it produces is deliverable from `Message` columns already on the model — `prompt_line_id` for which
text, a new column for which rendering.

## The one thing that is decided

**Do not reproduce another company's chat design.** An email styled as LinkedIn (or Slack, or
WhatsApp), sent from the operator's own domain, is the exact signature of a phishing kit: brand
impersonation is weighted heavily by every major receiver, so the message lands in spam rather than
in front of anyone, and it is trademark exposure on top of that. The *shape* of a chat — short
bubbles, a thread you can see — is free to borrow. The identity is not.

It is also the boundary this project's parent already drew once: OpenOutreach removed its browser
channel outright for **zero platform-ToS surface**, and a message dressed as a platform's own
reopens exactly that.

## The argument against the whole card, kept in front

**The mechanism is probably not the pixels.** What makes a LinkedIn message get answered is that the
recipient is already in that inbox with a persistent identity, the sender has a profile they can
check in one click, and replying is one keystroke where they already are. An email that links out to
an unfamiliar chat adds friction to the one channel that has none — hitting reply is the cheapest
action a cold recipient can take.

And the competing explanation is cheaper to test and already written down: the plays card's evidence
is that the gap between a message that gets answered and one that gets deleted is **offer, social
proximity and visibly-human text** — not depth of personalisation, and certainly not layout.

Both can be true. Only one of them costs a web surface.

## Nothing here is decidable yet

**"The reply rate went to the floor" is a feeling until the mail log makes it arithmetic.** Every
accepted send leaves a row and every inbound turn leaves one, so reply rate is countable per campaign
and — once plays exist — per play. Until that number is on screen, any of the three pieces above
would ship as a change nobody can grade.

So the prerequisite is not effort, it is **a denominator**. The program itself is no longer in the way
— [`p1-e2-outsend-ingest-and-packaging`](history/2026-08-27-p1-e2-outsend-ingest-and-packaging.md)
landed — so what remains is the reply-rate arithmetic over the log.

## User Story

**Persona:** an operator whose campaign is sending cleanly — inside the window, paced, from a warmed
box, with opt-outs honoured — and getting almost nothing back. The machinery is not the problem. The
message arriving as one more grey block of text in a stranger's inbox might be.

---

They open the campaign's numbers and see the reply rate for what it is, per play, with a denominator.
They turn on the one thing that costs nothing: their own face on the sending domain, so the client
renders it beside the subject the way it does for everyone the recipient already knows.

The next batch goes out identically otherwise — same plain text, same pacing, same box. A week later
the two cohorts are side by side, and the answer is a number rather than an opinion. If the face
moved it, the chat shape is worth trying next; if it did not, the message was never the problem with
the message, and the plays card owns what is.

---

**Single-sentence version:** As an operator, I want to test whether making the sender look like a
person — a face first, a conversation shape second — raises the reply rate, measured against a real
denominator, so that the expensive answer (a hosted place to reply) is only built if the cheap ones
fail.

## User Story — the reply-options piece

**Not built yet.** This describes piece 4 once the schema field, the HTML thread template, the
`mailto:` links, and the `conf.py` draw-rate flag all exist — see *Where this stands* above for what's
actually shipped today.

**Persona:** the same operator, running the same clean campaign, now deciding whether to risk the
plain-text advantage on a slice of it to find out if a chat-shaped thread actually gets answered
more.

---

They pin the HTML thread on for one cohort of an in-flight campaign, the same way `--prompt-line`
already lets them pin a text — nothing else about that cohort changes: same pacing, same box, same
prompt line, so any difference in reply rate afterward is attributable to the one thing that moved.

A lead in that cohort gets a follow-up. Instead of a paragraph, it renders as a thread: the opener
they already got, then this message, laid out as bubbles the way a LinkedIn conversation reads. Under
the model's own words sit two or three short lines, each one a complete reply in the lead's likely
voice — *"Sure, tell me more"*, *"Not right now"*, *"Who is this?"* — each a `mailto:` link built from
that text. The lead reads the thread, taps the one that's closest to what they'd say, and their mail
client opens with that reply already sitting in the compose box, addressed and worded, one click from
sent. They can edit it first, or just send it — the same one-keystroke feel as answering a DM, without
leaving their inbox or trusting a page they've never seen.

The reply lands back in the thread like any other inbound message and the outreach agent answers it
the same way it always has — `reply_options` only ever changes what is easy to send back, never what
counts as a reply once it arrives.

A week on, the operator reads the pinned cohort's reply rate against the plain-text control, on the
same denominator the rest of this card already requires. If it moved, HTML graduates toward being the
default draw; if it didn't, or if inbox placement dropped, the random draw stays plain-text and the
thread stays an option, not the default.

---

**Single-sentence version:** As an operator, I want each generated reply to ship with a few
one-tap, prewritten reply options — rendered as a LinkedIn-shaped thread, selectable per send the way
a prompt line already is — so a lead can answer as easily as they would a DM, and I can tell from the
reply-rate cohort split whether the HTML cost it me deliverability.

## Done when

- [ ] Reply rate is countable from the mail log, per campaign, with sends as the denominator.
- [ ] The operator's avatar is set up once and renders on the sending domain, with **no remote image
      in the message body**.
- [ ] Any presentation change ships as a cohort split against the current plain-text send, and is
      kept or reverted on that number.
- [ ] No message imitates another company's branding, in any variant, at any point.
- [ ] `OutreachDecision` carries `reply_options` — two or three short, complete replies in the
      recipient's likely voice, held to the same rules as `message` (length, no links, no machine
      tells) **stated in the prompt only** — and the HTML thread template renders them as `mailto:`
      links under the message, with the whole prior thread rendered above it as bubbles.
- [x] `cold_message_breach` and its retry (`COLD_ATTEMPTS`) are removed from `run_outreach_agent`; the
      same rules now carry their full weight from `outreach_agent.j2` alone. *(Done — the check-loop
      tests in `test_outreach.py` (`TestOpenerBreach`) are gone with it, since they tested a function
      that no longer exists; the prompt-content test (`test_the_hard_rules_are_in_the_prompt_whatever_
      the_line_says`) already covers the rules living in the template.)* **Still open:** a batch of
      real generations read by hand to confirm the model still holds the word ceiling and the
      no-link/no-em-dash rules without the check behind it — nothing has verified that yet.
- [ ] The HTML thread render is selectable per send (a draw rate in `core/conf.py`, defaulted to
      `0.0`, pinnable per invocation like `--prompt-line`), not a process-wide switch, so a cohort
      split is possible within one campaign. Plain-text stays the shipped default until the split says
      otherwise.
- [ ] A `mailto:` reply survives the two or three mail clients the lead base actually uses — subject,
      body, and any newlines arrive intact, not truncated or stripped.
- [ ] The subject line follows LinkedIn's own notification-email pattern with the platform name
      replaced (not reproduced) — wording itself (with or without naming the product) tested through
      the same cohort-split mechanism rather than decided up front.

## Open questions

- **Is a plain-text send with a domain avatar strictly better than an HTML one with an embedded
  image?** The whole first piece turns on this and it is answerable from the receivers' own docs.
- **Whose account does the hosted thread belong to?** If it is the operator's, it is a self-hosted web
  surface they now have to run; if it is ours, it is the paid tier
  ([`p2-e3-inbound-agentic-email`](p2-e3-inbound-agentic-email.md)) and this card is a feature of that
  one, not a card of its own.
- **Does a reply UI break the one-way boundary?** No — the boundary is between finder and sender, and
  the conversation is wholly the sender's. Worth stating because it looks like it might.
- **How many reply options, and for which stages?** A cold opener asks one question and nobody has
  written back yet, so there is no thread to render — piece 4 reads as a `follow_up`/`reply`-stage
  thing by default. Whether an opener gets options too (e.g. "yes" / "not interested" on the very
  first touch) is undecided.
- **What happens when a `mailto:` reply comes back edited, partially, or not at all?** The option is a
  *suggestion*, not a commitment — the lead can edit it, delete it, or ignore it and reply free-form
  as today. Nothing about the reply-ingestion path (`emails/steps/reply.py`) needs to know which
  happened; this is here so the design doesn't quietly assume the option is sent verbatim.
- **Does a `mailto:` link survive real inboxes?** Length is not the risk — a reply option is a short
  message under the same discipline as the body, and even the tightest client limit (Outlook desktop,
  historically ~2,000 characters for the whole encoded URL, since raised to ~8,192 in newer builds;
  Gmail ~4,096) comfortably fits one. The real risk is encoding: some clients have mishandled `+` or
  `%0A` inside `body=`, so a spot check across the two or three clients the lead base actually uses is
  still needed before trusting this as the primary path rather than a nice-to-have.
- **Subject line, decided in shape, open on wording:** reply rate was visibly higher when messages
  read as LinkedIn-native, and the decision is to borrow LinkedIn's own notification-email subject
  *pattern* — short, low-key, "someone messaged you" framing, e.g. `New message from {sender_name}` —
  with the platform's name stripped out and the product's own put in its place (`New message from
  {sender_name} via OpenOutreach`, or no platform mention at all). This is the same borrow-the-shape,
  not-the-identity line the card already draws for the chat bubbles: the *pattern* of a familiar
  notification subject is free to reuse, "LinkedIn" the word and mark are not. **Open**: whether naming
  the product in the subject at all undercuts the goal — the point of the pattern is that it reads as
  a personal notification, and "via OpenOutreach" is itself a tell that a system sent it, the same
  problem in different words. Worth testing subject variants (with/without product name) inside the
  same cohort-split mechanism as the HTML thread, rather than deciding this from the armchair.
