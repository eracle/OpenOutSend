"""The schema on the receiving end of the pipe.

Three tables, and each one answers a question the boundary contract asks:

- **Lead** — the person, keyed on the producer's own `lead_id`. Every field on it is
  a field of the record, so nothing here is derived and nothing is inferred.
- **Deal** — that person's conversation, one row per lead, carrying the finder's
  `reason`. This is also the row the send path walks. (What a message is written
  from — `product_docs`, `campaign_target`, `booking_link` — is not stored at all: it
  comes from `OUTSEND_*` on every run (`core/config.py`), and this install has never
  run more than one campaign, so there is nothing per-deal to hold it against.)
- **Suppression** — the addresses that may never be written to again. Address-keyed,
  terminal, and the one thing an erasure must not remove: forgetting that somebody
  opted out is how they get mailed again.

**The states are this side's.** The finder's funnel ends at `RESOLVED` — an address
in hand — and sending starts after that, so nothing here mirrors a finder state.
"""
from __future__ import annotations

from django.db import models
from django.utils import timezone


class Lead(models.Model):
    """A person the finder qualified, exactly as the record described them.

    **`lead_id` is the producer's key, and it is opaque here.** The finder emits its
    own primary key; this side never parses it, only joins on it — which is what lets
    a re-ingest be a correction rather than a duplicate, and what a bounce report would
    join back on the day an inbound endpoint exists.

    Every text field defaults to `""` rather than to null. The finder distinguishes
    *never told* from *empty* because it decides whether to go and find out; nothing
    on this side ever does, and a merge tag renders both the same way.
    """

    lead_id = models.CharField(max_length=64, unique=True)
    # The record's own field names — the importers', not ours, and kept unchanged
    # across the boundary so one vocabulary spans it.
    email = models.EmailField(blank=True, default="")
    first_name = models.CharField(max_length=100, blank=True, default="")
    last_name = models.CharField(max_length=100, blank=True, default="")
    company = models.CharField(max_length=200, blank=True, default="")
    title = models.CharField(max_length=200, blank=True, default="")
    website = models.CharField(max_length=500, blank=True, default="")
    linkedin_url = models.CharField(max_length=500, blank=True, default="")
    # The raw firmographic string the qualifier judged on — the facts a message is
    # written from. It crosses as text rather than as an extraction because
    # summarising *for a message* is this side's job: an opener wants the recent and
    # the specific where a verdict wants the durable, and one text derived twice with
    # no rule for which wins is drift waiting to happen.
    profile_text = models.TextField(blank=True, default="")
    # The facts extracted from that text — ``{"facts": [...]}``, built lazily on the
    # first touch and never rebuilt (`leads/summaries.py`). Deleting the blob is the
    # only way to ask for it again, and there is no reason to: the firmographics a
    # lead was discovered with do not change under us.
    #
    # **Per lead, not per deal.** The finder kept its version on the Deal and
    # conditioned the extraction on the campaign target, which is what made it tuned
    # for a verdict rather than for an opener. What an opener wants — the specific and
    # the distinguishing — is a property of the person, so it lives here.
    profile_summary = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.public_id

    @property
    def public_id(self) -> str:
        """How this person is named in a log line.

        The profile URL first, because it is the one identifier that is stable, unique
        and safe to print; the address only when there is no URL, and the producer's
        key when there is neither. A lead with no address is normal here — an
        exportable row is not a mailable one — so the fallback is not an edge case.
        """
        return self.linkedin_url or self.email or f"lead:{self.lead_id}"


class DealState(models.TextChoices):
    """Where a deal is in *this* side's funnel — the conversation, not the search.

    The finder's states describe finding somebody and optionally resolving an
    address; they all end before the first message is written. These three describe
    what happens after:

    - **READY** — ingested and not yet written to. A row with no address rests here
      too: an address is an enrichment a later run fills in for free.
    - **EMAILED** — at least one cold email went out and the conversation is live.
      A silent lead rests here between touches and leaves as `COMPLETED`
      (`unresponsive`) once its touches are spent; one who answers stays here while
      the thread runs. Which of the two a row is, is never stored — it is the
      comparison between the thread's newest inbound and outbound turns.
    - **COMPLETED** — the conversation reached its end, with an `Outcome` saying how.

    **The state says whether it is over; the outcome says why.** There used to be a
    terminal state for an opt-out and another for a dead address, which put two of the
    endings in this column and the other seven in `outcome` — so "why did this end"
    was answered by reading one column for some deals and the other for the rest.
    They are `unsubscribed` and `undeliverable` now, and the shape they moved to is
    the one `give_up` always used: end the deal, name the reason.

    **A state added here is unsendable by default.** Both pools name the state they
    want (`state=READY`, `state=EMAILED`) rather than excluding the ones they don't,
    so a terminal state cannot be re-selected by an exclusion somebody forgot.
    """

    READY = "Ready to Email"
    EMAILED = "Emailed"
    COMPLETED = "Completed"


class Outcome(models.TextChoices):
    """Why a conversation ended — the outreach agent's own vocabulary.

    These are *conversation* states, confounded by the sending skill, which is
    exactly why the boundary declines to send them back across the pipe. They are
    useful here, to the operator and to the learner over the send log, and nowhere
    else.

    **The last two are not the agent's**, which is why they are listed apart: the
    agent may choose any of the first seven, and these two are written by the code
    that learns the fact. They are outcomes all the same — a deal that ended because
    somebody asked to stop ended for a reason, and the reader of a funnel should not
    have to know that two of the nine are kept in a different column.
    """

    CONVERTED = "converted"
    NOT_INTERESTED = "not_interested"
    WRONG_FIT = "wrong_fit"
    NO_BUDGET = "no_budget"
    HAS_SOLUTION = "has_solution"
    BAD_TIMING = "bad_timing"
    UNRESPONSIVE = "unresponsive"

    # Distinct from `not_interested`, which is a verdict on the offer: this one is a
    # verdict on being written to at all, and it is the only outcome carrying a duty —
    # the address goes on the account-wide list and stays there.
    UNSUBSCRIBED = "unsubscribed"
    # The receiver said there is nobody at that address. Deliberately not
    # `unsubscribed`: nobody asked for anything, and a row reading as withdrawn consent
    # when it is really a dead mailbox misstates what happened to whoever reads the
    # funnel later.
    UNDELIVERABLE = "undeliverable"


class Deal(models.Model):
    """One lead's conversation — the row ingest writes and the send path reads.

    `lead` is the identity key, one-to-one, because idempotency is the whole reason
    the pipe is allowed to be lossy: a re-ingest has to land on the same row or
    recovery-by-re-running is a way of mailing people twice.
    """

    class Meta:
        indexes = [
            models.Index(fields=["state"], name="deal_state_idx"),
        ]

    lead = models.OneToOneField(Lead, on_delete=models.CASCADE, related_name="deal")
    state = models.CharField(max_length=32, choices=DealState.choices, default=DealState.READY)
    outcome = models.CharField(max_length=20, choices=Outcome.choices, blank=True, default="")
    # Why the LLM chose this lead, in its own words. **Operator-facing**: it is the
    # evidence for the person running this, third-person and evaluative, and it never
    # goes in a message. The mail is written from `Lead.profile_text` instead.
    reason = models.TextField(blank=True, default="")
    # When the finder wrote that verdict — the record's own provenance, kept so a row
    # can be told from a re-qualification of it later.
    qualified_at = models.DateTimeField(null=True, blank=True)

    # ── The conversation, once there is one ───────────────────────
    #
    # All four are written by the send step and stay empty until then. The deal points
    # at the thread rather than copying it: the mail log is the record of what left the
    # box, so nothing about a message is stored twice and nothing can disagree with
    # itself later.
    mailbox = models.ForeignKey(
        "outsend_emails.Mailbox", null=True, blank=True, on_delete=models.SET_NULL, related_name="deals",
    )
    thread = models.ForeignKey(
        "outsend_emails.Thread", null=True, blank=True, on_delete=models.SET_NULL, related_name="deals",
    )
    email_subject = models.CharField(max_length=500, blank=True, default="")
    email_sent_at = models.DateTimeField(null=True, blank=True)
    # What this conversation has taught us — ``{"facts": [...]}``, rolled forward as
    # each reply is read (`leads/summaries.py`). Per deal, because it is per
    # conversation, and a thread is what the agent is answering inside.
    #
    # It is not a transcript: the mail log holds every message, and the agent reads
    # the last few turns verbatim beside this. What the fact list carries is the part
    # of the thread that has scrolled out of that window and still matters — which is
    # the whole reason a summary exists rather than a bigger window.
    chat_summary = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.lead} [{self.state}]"


class Suppression(models.Model):
    """An address that may never be written to again. Terminal, by design.

    Keyed on the address rather than on the lead, because that is what the duty
    attaches to. It is also why ingest re-checks suppression whenever an address
    *changes* — a corrected address is an unsuppressed one until something looks
    again.

    **Nothing deletes from this table.** An erasure request reaches every other store
    on this side; this one it must leave alone, since forgetting that somebody opted
    out is how they get mailed again.
    """

    email = models.EmailField(unique=True)
    # Where the opt-out came from — a worded reply, the `+unsub` alias, a bounce.
    # Free text: it is read by a person looking at a row, never branched on.
    reason = models.CharField(max_length=200, blank=True, default="")
    suppressed_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return self.email
