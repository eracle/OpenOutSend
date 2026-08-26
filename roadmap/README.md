# Roadmap

**Seven of these cards were inherited from OpenOutreach on 2026-08-19**, when that project stopped
sending email and its sending half was ported here (see [`../cold_outreach/`](../cold_outreach/)).

They are not a wishlist someone wrote for this repo. They are **open work that followed its subject
matter across the boundary** — each one describes code that now lives in `cold_outreach/`, and each was
filed against a running system by someone who had hit the problem. Three of them describe defects that
are still live.

**The eighth is new**, written in openoutreach-docs and filed here for the same reason the seven came:
the subject is here. It is the only card in the folder designed *after* the port rather than before it.

**The ninth is the one to read first.** Every other card describes code that cannot run yet: there is no
console script, no settings module and no database, and the send path still reaches into OpenOutreach's
CRM for the row it acts on. `p1-e2-outsend-ingest-and-packaging` is what makes the rest buildable.

**The tenth is a hypothesis rather than a design**, filed so the reasoning behind it is not lost:
whether the reply rate answers to how the message *looks* — a face, a chat shape, somewhere to reply
that is not an inbox. It is gated on being able to count a reply rate at all.

## The cards

| Card | What it is | Why it came here |
|---|---|---|
| `p1-e2-email-bounce-detection-suppression` | The send path has no feedback from delivery failure | **The most valuable one.** A bounce is the only verdict on whether an address was real, and the finder can no longer see one. Half the mechanism already exists in `warmth.py` |
| `p1-e2-inbound-mail-silent-skip` | The mail pass can skip a message and nothing notices | **A live bug.** Came across unfixed with `sync.py` |
| `p2-e2-followup-identity-backoff-sentiment` | Three defects in the outreach agent | The agent is `cold_outreach/core/agents/outreach.py` now |
| `p3-e1-onboarding-daily-send-cap-prompt` | Ask the operator for a daily send cap | Its premise moved — the cap is *measured*, not configured. Rewrite before building |
| `p3-e2-mailbox-oauth-authentication` | What mailbox OAuth would cost | Its trigger (Google restricting app passwords) now breaks *this* project and nothing else |
| `p3-e2-resend-opt-in-send-transport` | Resend as an alternative transport | Sending lives here |
| `p2-e3-inbound-agentic-email` | Hosted reply capture + agent autopilot, as a paid tier | The only *product* idea in the folder, and a sender's product |
| `p1-e2-outsend-ingest-and-packaging` | Ingest on stdin, the lead model behind it, and `outsend` as a command | **Written here, and the prerequisite for every row above.** Nothing in this repo runs until it lands |
| `p1-e2-sender-message-generation` | Plays as files, fragments as the database, a learner over observed replies | **Not inherited — written after the port.** It reshapes `core/agents/outreach.py` and its prompt template, and it is what the low reply rate on the first version actually needs |
| `p2-e2-message-carries-a-face` | Whether presentation moves the reply rate — an avatar, a chat shape, a hosted thread | **Written here, from a live complaint.** A hypothesis with a cheaper rival already on the table, so it is gated on measuring a reply rate rather than on effort |

## Reading them

Each card opens with a header note saying where it came from and what changed on the way over. **Read
that note before the card body** — several of the seven were written against assumptions the port
invalidated, and the note is the only place that says so. A card whose body contradicts its header is
not a puzzle: the header is newer. The eighth card's header says the opposite, and that is the point of
having one: nothing in it predates the code it describes.

Two conventions came with them and are worth keeping:

- Filenames are `pN-eN-title.md` — priority (`p1` high → `p3` low), then effort (`e1` low → `e3` high) — so `ls` sorts by what to do first.
- A card that gets built moves to `history/`, date-prefixed. A card that dies because its subject no longer exists moves there too, but **must say at the top that it was not built** — otherwise the archive reads as a list of delivered work, which is a lie with a date on it. That distinction is exactly why these seven are here rather than in OpenOutreach's history folder: they were never finished, and filing them as finished would have buried real open work.

## What did *not* come across

`p1-e3-status-queue-dispatcher` — the card behind OpenOutreach's daemon cycle. It genuinely shipped
there, and the cycle is the finder's, not this project's. It stayed in that repo's history.

The **freemium promotional campaign** is not here and must not be rebuilt here. It mailed an
advertisement for OpenOutreach from the operator's own mailbox, under their identity; it was deleted
as an install tax rather than a growth loop. `../cold_outreach/README.md` says the same thing, and
that is deliberate duplication.
