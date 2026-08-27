# The Message Is Composed From Plays — Files In Git, Fragments In A Database, Learned From Replies

> ## ✍️ Written in openoutreach-docs, filed here because the subject is here.
>
> **Not inherited from the 2026-08-19 port** — the other seven were. This one was designed after it,
> against the code as it actually stands in `cold_outreach/`, so nothing in the body was written under
> assumptions the port invalidated. There is no older version of it in another repo.
>
> It reshapes what is already built: `cold_outreach/core/agents/outreach.py` and
> `core/templates/prompts/outreach_agent.j2` are the generator, and **prompt lines replace that one
> template** rather than arriving beside it.
>
> Two cards in this folder are prerequisites, not neighbours —
> [`p1-e2-inbound-mail-silent-skip`](p1-e2-inbound-mail-silent-skip.md), because replies are the
> learner's only reward and a skipped inbound is a reward that never arrives, and
> [`p1-e2-email-bounce-detection-suppression`](p1-e2-email-bounce-detection-suppression.md), which
> owns half the suppression duty the pipe contract leans on.

- **Status:** In Progress — **the shape half is built and the log under it is collecting.** Six
  **prompt lines** ship as files (`cold_outreach/prompt_lines/`), one is drawn at random per opener or
  pinned with `outsend send --prompt-line ID`, the hard rules are enforced in the generator rather than
  repeated in every file, and every send records which line wrote it and a hash of that line's text.
  **The learner is deliberately not built**: the reward is roughly one bit per fifty sends arriving
  days late, so it would fit on nothing for months, while a random draw collects exactly the data it
  will eventually need. What is left is the extraction retune, the fragment split that only matters
  once something scores it, and the threshold that says a flat log means *the material is thin* rather
  than *shape does not matter*.
  **Receiver-internal**: everything here lives on the sender's side of
  [the boundary contract](https://github.com/eracle/openoutreach-docs/blob/main/roadmap/p1-e2-find-send-boundary-contract.md) and nothing in it crosses the pipe. Two things are
  **explicitly ditched** (federated pooling, Optuna/TPE). The input problem in *What we can actually
  say* is still the open one, and it bounds everything else.
- **Priority:** High — the first version's reply rate was low, and the message is the half the whole
  split was made to hand away.
- **Effort:** Medium
- **Area:** OpenOutSend — message generation, the send log, and the learner over it.

> **Trigger** *(Eracle, inbound)*: a cold message that got a reply, quoted whole because what made it
> work is not what we assumed.
>
> ```
> Buenas tardes Antonio Ercole.
> Soy David, de la networking Akuaro.
>
> Te contacto porque estoy trabajando distintas posiciones y creo que tu perfil encaja bien en estas.
> He podido conocer gente de tu empresa y se que el satck tecnologico encajaria bien en estas
> posiciones que estoy trabajando, asi que si estubieras abierta a escuchar ofertas quedo a tu
> disposicion para comentarlas.
> ```

## What made that message work

**It carries less personalisation than we can already produce.** A name, an employer, a vague gesture
at *el stack tecnológico*. Nothing the sender could only know by paying attention. That is the
uncomfortable part and it has to stay in front of the design: the gap between this message and the
ones that get deleted is **not** depth of personalisation.

What it has instead:

| | |
|---|---|
| **An offer valuable independent of fit** | *"I have positions and yours might fit"* is worth something to nearly any employed engineer. A recruiter gets this free; we do not. |
| **Social proximity** | *"He podido conocer gente de tu empresa"* — cheap to say, disproportionate on trust. |
| **Visibly human** | `satck`, `estubieras`, `abierta` for a man, no accents. Someone typing fast, not a system. Clean prose is now itself a spam signal. |
| **One low-commitment ask** | No link, no calendar, no pitch about Akuaro. |
| **The recipient's language** | Spanish, to an Italian name, in Spain. |

**The offer is the term we cannot copy and the one that probably mattered most.** Reply rate is
dominated by list quality × offer × timing, and the message is a smaller term than any of them. This
card optimises the smaller term on purpose, with that stated, because it is the term we control.

## What we can actually say — the input problem

`profile_text` is built by `discovery.py:profile_text_for` from exactly eight fields
(`discovery.py:TEXT_FIELDS`) — `contact_headline`, `contact_industry`, `contact_job_title`,
`company_name`, `contact_seniority`, `company_industry`, `contact_location_state`,
`contact_location_country` — joined and lowercased. **That string is what crosses the pipe, and it is
the whole raw material.**

**The sender does its own extraction.** The finder builds no summary: `core/db/summaries.py` has no
consumer there, the qualifier reads `profile_text` directly, and the module is the sending leg's own
code left behind by the split — so it moves here along with `core/vendor/mem0`, and
[the boundary contract](https://github.com/eracle/openoutreach-docs/blob/main/roadmap/p1-e2-find-send-boundary-contract.md) carries the reasoning. That is not just tidier ownership, it is
the only way the extraction can be tuned for this job: the inherited prompt prefers *"concrete, durable
facts (identity, role, employer, location, career arc, stated goals, expressed concerns) over fleeting
commentary"*, which is right for a verdict and backwards for an opener.

But retuning it cannot conjure what the input lacks. From those eight fields any extraction yields
*"X is a CTO"*, *"X works at Acme"*, *"X is in Belgium"*.

**The best message obtainable from that is "I saw you're a CTO in fintech in Belgium"** — the
archetypal ignorable opener, and one every competitor can also write, because they buy the same fields
from the same kind of provider. A message that reads like a LinkedIn DM references something the
person *chose to publish*, and we hold nothing they chose. **This is an input problem before it is a
prompt problem**, and no amount of generator tuning manufactures the missing material.

### Decided: ship on firmographics, fetch nothing yet

**The first version writes from the eight fields and nothing else.** Only *change the shape, not the
inputs* is in scope — see *Hard-coded* — because it is free and right regardless of what the log later
says.

**The consequence is on the record, so it is not mistaken for a result:** every play is a phrasing of
the same thin material, and the learner is comparing phrasings rather than messages that differ in
what they know. **A flat log is therefore the expected outcome, not evidence that message shape does
not matter** — it is evidence that shape alone is not enough, which is a different finding and the one
this build is actually testing.

Two sources are deferred, both fetched **at send time in the sender** when they arrive — per-lead work
paid only for people actually mailed, rather than across the whole discovery funnel:

- **Company pages.** The record carries `website`; a pricing page, a careers page, a changelog or an
  announcement is public, cheap, and legally quiet. *"You're hiring three backend engineers in
  Lisbon"* is specific and reads as attention paid. **The cheapest way to make the material thicker,
  and the first thing to reach for when the log says shape alone did not move.**
- **What the person chose to publish.** Posts, a talk, a podcast. Highest value for reply rate and the
  only route to a genuine LinkedIn-DM feel — and the one needing a deliberate decision rather than a
  casual one, alongside `LEGITIMATE_INTEREST_ASSESSMENT.md` and the second-store question
  [the boundary contract](https://github.com/eracle/openoutreach-docs/blob/main/roadmap/p1-e2-find-send-boundary-contract.md) settles.

## Plays are files; fragments are the database

**A play is a file in git.** Few, hand-authored, reviewed, diffed, versioned — config, not data. What
lives in the database is the *log* and the per-fragment statistics over it. This is the same split
already drawn between campaign config and CRM rows.

**A prompt line encodes a move, not a mail-merge template.** The moment a file contains
`{{first_name}}, I noticed {{company}}…` we have rebuilt Instantly and discarded the reason to have a
model in the loop. A file carries the move and why it works, the ask shape, and which leads it suits.
The length ceiling and the forbidden list are **not** in it — they are enforced in the generator, so a
dozen files do not have to remember them and no file can drop one by being edited carelessly.

**But the format permits fixed text too.** The trigger message worked partly *because* the words were
a human's clumsy ones, and a model writing from a posture produces clean prose. Some lines will be
mostly skeleton, some pure posture, and **the ratio is itself one of the things the learner varies.**

**The fragments in *What made that message work* are the database.** Each is a small, named, reusable
prompt piece — the offer frame, the proximity claim, the ask shape, the register — and a prompt line is
a composition of them. The fragment is the unit the learner scores; the line is the unit a person reads
and edits. *(Deferred: a line is one whole piece of text today, and the fragment only earns its
complexity once something scores it.)*

**Identity is the id and the text.** `(lead_id, prompt_line)` is the key
[the boundary contract](https://github.com/eracle/openoutreach-docs/blob/main/roadmap/p1-e2-find-send-boundary-contract.md)
already floats for follow-ups, and a digest of the prompt rides beside it so an edited line does not
pool with the version before it.

**Selection is a random draw**, with `--prompt-line` pinning one — named exactly as `--campaign` is,
and the sender narrates what it used. One vocabulary across the whole system. Random rather than
"required only if there are several", because the draw *is* how the comparison data gets collected;
pinning is the deliberate exception, not the default.

**Ship several, score none.** The format and the log are the work. Six lines ship because a library
costs nothing here — they are prompt text — while a scorer over them costs months of waiting.

## Hard-coded, not tested

These are constraints, not hypotheses. We already believe them, they cost nothing, and spending
statistical budget on them buys nothing:

- **Write in the recipient's language.** `contact_location_country` is already in hand, and this is
  plausibly a larger lever than any prompt tuning.
- **No link and no pitch in the first message.** Ask a question that is cheap to answer, not for a
  meeting — the same discipline as the Mom Test, applied to cold email.
- **A length ceiling.** Around 75 words. Most of the LinkedIn-DM *feel* is length and ask-shape, not
  personalisation depth.
- **Sourced claims only.** *"I've met people from your company"* is powerful **because it is true when
  a human says it.** A play instructing a model to claim acquaintance manufactures a small lie for a
  thousand strangers. Any assertion about the recipient's world comes from a field, never from the
  model.
- **No faked typos.** Manufactured incompetence is a trick that cannot be taken back once the same
  "mistake" turns up in two inboxes. Strip the machine tells instead — no *"I hope this finds you
  well"*, no em-dashes, no three-item lists, no perfectly balanced clauses. Plain and slightly uneven
  reads human because it is.

## The learner

**Kept: active learning over fragments.** The unit is the fragment, not the whole play, and the model
is **additive fragment effects with shrinkage** — main effects first, interactions only where the data
supports them. That is what makes the fragment database tractable at all: the data requirement is
**O(fragments)**, not **O(2^fragments)**, which a flat bandit over whole compositions would demand.
Sampling is posterior-based (Thompson), which handles a sparse Bernoulli reward natively and gets
explore/exploit right without a hand-tuned schedule.

**Keep a slice of forced randomisation.** Once the sampler chooses, the data is collected under its
own policy and the estimates stop being honest. A small always-random fraction is the fix.

**The reward is observed replies. Only.** No surrogate judge. The consequence is on the record: the
signal is roughly one bit per fifty sends, arriving days late, so **the loop moves slowly and its early
posteriors will be wide.** It is expected to be uninformative for a long while, and that is not a bug
to debug. The alternative — a calibrated LLM judge as a cheap surrogate — was considered and declined;
it can be reopened if the log stays flat.

**Ditched: federated pooling through the hub.** It would have dissolved the volume problem, and it is
not being built.

**Ditched: Optuna / TPE.** TPE splits observed trials by a quantile and models `l(x)/g(x)`, which
assumes each trial returns a scalar with **low noise** — a training run's validation loss. A reply rate
at n=100 is nearly pure noise, so the good/bad split TPE depends on would be close to random. It is
the wrong noise regime, not the wrong idea.

**The precedent is in our own code.** The GP was the frontier ranker until *"§13 measured the GP losing
to plain counting"*, and query selection is arithmetic now (`discovery.py:keyword_terms`). A learner in
a data-starved regime lost to counting once already. Ship the log first and let it say whether there is
signal.

## The log

One row per send: lead, play, the fragment set, the segment keys (seniority, country, industry),
whether a reply came back. **Aggregate-shaped from day one** — nearly free now, expensive to retrofit
once there is history.

Reading replies is what closes the loop, and it is the reason the sender needs a cadence at all. **What
shape that cadence takes — a resident daemon, or a second bounded verb on a timer — is open** and is
tracked in [the boundary contract](https://github.com/eracle/openoutreach-docs/blob/main/roadmap/p1-e2-find-send-boundary-contract.md), because it is also the precondition for
`openoutreach[send]`.

## What already exists on the receiver

Not a green field. `cold_outreach/core/agents/outreach.py` and
`core/templates/prompts/outreach_agent.j2` are the generator this card reshapes — **prompt lines
replace that one template**, they do not arrive beside it. `emails/steps/send.py` and `emails/steps/reply.py` are
the two passes, and the reply pass is what the learner's reward depends on.

**Two of the seven cards inherited by that repo are prerequisites, not neighbours:**
`p1-e2-inbound-mail-silent-skip` is a live defect in the very path that observes replies — with replies
as the only reward, a skipped inbound is a reward that never arrives and a fragment that looks worse
than it is — and `p1-e2-email-bounce-detection-suppression` owns the other half of the suppression duty
the pipe contract leans on.

## Gaps this card has to close or name

- **The trigger for reaching for company pages.** Shipping on firmographics is decided; what is not is
  *what result sends us to the next source*. Without a stated threshold, a flat log gets read as "the
  learner does not work" instead of "the material is too thin", and the wrong thing gets rebuilt.
- ~~**A prompt line's identity has to survive its text changing.**~~ **Closed.**
  `Message.prompt_line_digest` is a hash of the prompt, written beside the id on every send, so an
  edited line is distinguishable from the version before it rather than pooled with it.
- ~~**Where prompt lines live on disk.**~~ **Closed.** Shipped defaults in
  `cold_outreach/prompt_lines/`, an operator's in `state_dir()/prompt_lines/`, and **the operator's
  wins** on a shared id — so a shipped line can be overridden without editing an installed package or
  losing the edit on upgrade. An edited copy keeps its identity through the id and is told apart by
  the digest.
- **The conversation lane is gone.** `Deal.chat_summary` and `update_chat_summary` left with the
  sending leg; the removal notes survive in `core/db/summaries.py`. A sender that holds threads
  rebuilds it.
- **Volume.** With replies as the only reward and no pooling, the arithmetic has to be stated in the
  card that proposes acting on the learner's output, not discovered later.

## Acceptance criteria

- [ ] Fact extraction runs in the sender, from the `profile_text` the pipe delivered, tuned for an
      opener rather than for a verdict. *(It runs here — `leads/summaries.py` — but on the inherited
      prompt, which prefers durable facts because it was written for a **verdict**. The retune has not
      happened.)*
- [x] **A prompt line is a file, with an id, and six ship.** *(`cold_outreach/prompt_lines/*.toml`,
      loaded by `core/prompt_lines.py`: shipped defaults plus `~/.openoutsend/prompt_lines/`, the
      operator's copy winning on a shared id. **Named "prompt line", not "play"** — it is a piece of
      prompt, and naming it for what it is beat naming it for a metaphor out of sales playbooks, which
      is the register this project avoids.)*
- [ ] Fragments are named, stored, and composable; the fragment is what the learner scores.
      **Deferred with the learner** — a prompt line is one whole piece of text today. The fragment
      only earns its complexity once something scores it, and splitting first would build the harder
      half for a reader that does not exist.
- [x] The hard rules are enforced in the generator, not left to a prompt line to remember — language,
      length, no link, no meeting request, sourced claims, no machine tells. *(In the template for
      every rule, and in `opener_breach` for the three a machine can actually check: the word ceiling,
      links, em dashes. A breach retries once with the complaint appended, then fails the send —
      quietly mailing something that breaks the discipline is the outcome worth avoiding. Language,
      register and sourcing stay prompt-side, because no regex separates a sourced claim from an
      invented one and pretending otherwise would be worse than the honest gap.)*
- [x] Every send records which prompt line wrote it, shaped so it aggregates.
      *(`Message.prompt_line_id` + `prompt_line_digest` — **both**, because an edited line keeps its id
      and stops being the same message. Whether a send was answered is already derivable from the
      thread, so the comparison is a query over two columns rather than a second log to keep in step,
      and it only works from the first send onward, which is why the columns exist before anything
      reads them. Segment keys sit on the lead and are joinable; they are not copied.)*
- [ ] The learner scores fragments with shrinkage and samples from the posterior, with a forced-random
      slice. **Deliberately not built yet.** Selection is a uniform random draw per send, and this
      card's own arithmetic is why: roughly one bit per fifty sends, arriving days late. A learner
      shipped today fits on nothing and stays uninformative for months, while the random draw collects
      exactly the data it will need. The precedent is in the finder — the GP lost to plain counting
      once already, and query selection is arithmetic now.
- [x] No surrogate judge, no pooled data, no TPE — and the reason each was declined is readable here
      rather than rediscovered. *(Nothing scores a message but a human replying to it.)*

## What is left

Two things, and they are the same thing twice: **the material is thin, and there is nothing to learn
from yet.**

The prompt lines vary the *shape* of a message written from eight firmographic fields. That was the
decision on the record — *"a flat log is the expected outcome, not evidence that message shape does not
matter"* — and it is now testable rather than merely asserted. What sends us to company pages is a flat
log across six lines with enough sends behind it, and **that threshold is still unstated**: it is the
gap named in *Gaps this card has to close or name*, and this pass did not close it.
