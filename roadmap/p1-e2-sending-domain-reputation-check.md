# The Operator Cannot Ask Whether Their Own Domain Is Blocked

> ## 📥 The last open criterion of [`p1-e2-email-bounce-detection-suppression`](p1-e2-email-bounce-detection-suppression.md), split out because the rest of that card closed.
>
> That card asked for two answers: *"what is my bounce rate?"* and *"is my domain listed?"*. The first
> is arithmetic the mail log can already do. The second is not built at all, and this card is it.

- **Status:** To Do
- **Priority:** High — the failure it prevents has already happened twice, and the second time was
  caught by hand.
- **Effort:** Small
- **Area:** OpenOutSend — a reputation read over the sending identity, and somewhere to show it.

## The incident that produced this card

**2026-08-27.** Preparing to resume sending, the operator's configured mailbox was
`eracle@indieoutreach.app`. Before enabling it, an assistant ran the query by hand:

```
multi.surbl.org      indieoutreach.app → 127.0.0.64   listed (abuse)
dbl.spamhaus.org     indieoutreach.app → 127.0.1.2    listed (spam domain)
zen.spamhaus.org     not listed
```

**Still listed, three weeks after the 2026-08-06 incident.** Authentication was clean the whole time —
SPF, DKIM, DMARC, Google MX — so nothing the operator could see said anything was wrong. The plan that
day was to resume cold outreach from that domain and would have gone ahead unquestioned.

**Nothing in the product would have said a word.** `warmth.py` measures the box's own bounce rate and
halves capacity above tolerance, which is a *lagging* signal: it reacts once mail is already failing.
A blocklist entry is the thing that makes mail fail, and it is public, free to query, and invisible to
this system.

The same session found the other half of why that domain got listed: the old install recorded **zero
bounces across 729 sends** and misfiled three non-delivery reports as human replies, so its true bounce
rate was never measurable by anyone. Suppression now fixes the cause. This card fixes the *noticing*.

## User Story

As an operator about to send cold email, I want the tool to tell me whether my sending domain is on a
public blocklist — before it sends anything — so that I find out from my own software rather than from
a silent collapse in reply rate, and so that I never resume sending from a domain that is already
burned.

**And once it is listed, I want to know it is still listed**, so that "we fixed the bounces" and "we
are delisted" stay separate facts and I can tell when the recovery has actually worked.

## What to build

### 1. The check

A DNS query per list, over the **domain** of the mailbox's `from_address`. Listed means an `A` record
exists at `<domain>.<zone>`; unlisted means `NXDOMAIN`.

| list | zone | what a hit means |
|---|---|---|
| Spamhaus DBL | `dbl.spamhaus.org` | the domain itself is classed as spam-associated |
| SURBL | `multi.surbl.org` | the domain appears in spam bodies; `127.0.0.64` is the abuse list |

**IP blocklists are deliberately out of scope.** Mail leaves through Gmail's or a relay's addresses,
so the sending IP's reputation is Google's and not the operator's — checking `zen.spamhaus.org` would
report on somebody else's asset and teach the operator nothing they can act on. Domain reputation is
the part they own, and the part they can lose.

### 2. Two traps that will otherwise be read as answers

- **A public resolver poisons the result.** Spamhaus refuses queries arriving from large open
  resolvers (`8.8.8.8`, `1.1.1.1`) and answers `127.0.1.255` — *query blocked*, not *listed*. Read
  naively that is a false positive on every domain. The return code has to be interpreted, not merely
  tested for existence, and a blocked query must report **unknown**, never listed and never clean.
  A VM whose `/etc/resolv.conf` points at a cloud metadata resolver may hit exactly this.
- **A consumer mailbox has no domain reputation to check.** Querying `gmail.com` will always come back
  clean and means nothing — the operator shares that reputation with everyone. For a consumer address
  the honest answer is *"you have no domain of your own; a suspension here takes the mailbox with it"*,
  not a green tick.

### 3. Where it surfaces

**`outsend status`**, a third verb beside `init` and `send`, mirroring the finder's. It answers without
sending: which mailbox, its measured capacity and headroom, its bounce rate from the mail log, and the
reputation of its domain. The bounce-rate half needs no new data — `report.bounce_rate` already exists
and nothing shows it to anybody.

**And once per send pass**, cheaply, so a listing that appears mid-campaign is noticed within a pass
rather than at the next time somebody thinks to look.

### 4. What it must not do

**It must not stop sending on its own.** A DNS timeout, a rate-limited resolver, or a mirror having a
bad day would silently halt a campaign, and the operator would have no idea why. Say it loudly, in the
pass's narration and in `status`; leave the decision with the person. This is the same rule
`warmth.py` follows — it *reduces* capacity, it does not refuse to run.

## Done when

- [ ] `outsend status` reports, for each configured mailbox: bounce rate, remaining headroom, and
      whether its domain is listed on DBL and SURBL.
- [ ] A blocked or failed query reports **unknown** and is visibly different from **clean**.
- [ ] A consumer mailbox (`gmail.com` and friends) is described as having no domain of its own rather
      than being reported clean.
- [ ] A send pass surfaces a listing without refusing to send.
- [ ] No test reaches the network — the resolver is mocked at the boundary, with the real return codes
      (`127.0.1.2`, `127.0.0.64`, `127.0.1.255`, `NXDOMAIN`) as the cases.

## Not this card

- Requesting delisting. That is a form on someone else's website and a human decision.
- Warming a new domain, or choosing one. Separate, and mostly not software.
- Postmaster Tools / seed-list placement testing — richer signals, both needing volume this project
  does not have yet.
