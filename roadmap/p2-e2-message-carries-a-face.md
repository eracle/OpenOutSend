# The Message Carries a Face — An Avatar, a Chat Shape, and Maybe a Thread to Reply In

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
- **Effort:** Medium for the first two pieces, High for the third.
- **Area:** Message presentation — `cold_outreach/emails/sender.py` builds the message today, and it
  builds a plain-text one.

## The idea, in three separable pieces

**1. The sender has a face.** Attach the operator's own avatar to the message so the recipient sees a
person rather than an address. Nothing about the lead is involved and **nothing new crosses the
pipe** — this is the operator's picture, configured once, exactly like their signature. The boundary
contract is untouched.

**2. The message has a chat shape.** Short bubbles, a visible thread, the layout of a conversation
rather than of a letter. Borrowed from what a chat UI looks like, not from whose chat UI it is.

**3. There is somewhere to reply that is not the inbox.** A self-hosted page showing the whole
conversation, where the lead can answer without composing an email.

## What each one actually costs

| | The cost that decides it |
|---|---|
| **Avatar** | The message stops being plain text. Gmail and Outlook both block remote images by default for an unknown sender, so the face is *absent* in the case that matters most — first contact — and a remote image in a cold email is also the shape of a tracking pixel, which is a spam signal in itself. An **inline attachment** avoids the remote fetch and adds weight and a MIME part. The cheap version of this idea is not an image at all: it is a Gravatar-backed **BIMI/avatar on the sending domain**, which the client renders itself from a source it trusts. |
| **Chat shape** | HTML. Every plain-text send today is a deliverability asset — plain text is what a person typing actually produces, and the plays card's own evidence is that visibly-human beats polished. An HTML template is the opposite signal, and multipart doubles the surface a filter reads. |
| **Hosted thread** | A web surface, inbound HTTP, a per-lead identity token, hosting and TLS, and a second store of conversation content with its own erasure duty. `outsend` deliberately has none of that: it is a CLI behind a timer with no web surface, which is what makes it a `pip install`. **This piece does not belong in this repo** — it belongs on the hub, which already has Django, Traefik and a domain, and it overlaps hard with [`p2-e3-inbound-agentic-email`](p2-e3-inbound-agentic-email.md), which is the same web surface filed as a paid tier. |

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

## Done when

- [ ] Reply rate is countable from the mail log, per campaign, with sends as the denominator.
- [ ] The operator's avatar is set up once and renders on the sending domain, with **no remote image
      in the message body**.
- [ ] Any presentation change ships as a cohort split against the current plain-text send, and is
      kept or reverted on that number.
- [ ] No message imitates another company's branding, in any variant, at any point.

## Open questions

- **Is a plain-text send with a domain avatar strictly better than an HTML one with an embedded
  image?** The whole first piece turns on this and it is answerable from the receivers' own docs.
- **Whose account does the hosted thread belong to?** If it is the operator's, it is a self-hosted web
  surface they now have to run; if it is ours, it is the paid tier
  ([`p2-e3-inbound-agentic-email`](p2-e3-inbound-agentic-email.md)) and this card is a feature of that
  one, not a card of its own.
- **Does a reply UI break the one-way boundary?** No — the boundary is between finder and sender, and
  the conversation is wholly the sender's. Worth stating because it looks like it might.
