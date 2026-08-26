"""The two questions a send pass asks the database: who is waiting, and who wrote back.

Both are **derived from state that already exists** — a deal's own state and the mail
log's timestamps. Nothing is minted, no queue table is written, and no row's timestamp
gates another's, which is what lets a pass be interrupted anywhere and simply asked
again.
"""
from __future__ import annotations

from django.db.models import F, OuterRef, Q, Subquery

from cold_outreach.leads.models import Deal, DealState


def emailable_deals(campaign):
    """Deals waiting for their first email, oldest first.

    Three conditions and no more: the state, an address to send to, and the campaign.
    **Suppression is not a fourth filter** — an opt-out moves every open deal for that
    address to `UNSUBSCRIBED` when it lands, and ingest parks a suppressed row there on
    arrival, so the state already carries the answer. `emails/sender.suppressed` asks
    the list again after the agent has written, which is the check that catches an
    opt-out arriving in the seconds an LLM call takes.

    Oldest first, so a lead who has been waiting a week is not overtaken by one
    ingested this morning.
    """
    return (
        Deal.objects.filter(campaign=campaign, state=DealState.READY)
        .exclude(lead__email="")
        .select_related("lead", "campaign")
        .order_by("created_at")
    )


def unanswered_replies(campaign):
    """`EMAILED` deals whose newest inbound turn is newer than our newest outgoing one.

    **This is the entire follow-up trigger.** No timer, no flag, no bookkeeping: the
    mail pass writes inbound rows, and the comparison between two timestamps says
    whether the ball is in our court. Oldest reply first, so nobody waits behind a
    livelier thread.

    **Turns, not messages.** A bounce and an out-of-office sit in the thread and are
    not somebody writing back, so neither can make a deal actionable — which is what
    stops an agent apologising twice to a dead address.

    **The two timestamps are subqueries, not aggregates.** `Max()` over the joined
    messages reads the same and turns the query into a `GROUP BY` over every selected
    column — including everything `select_related` just widened it with. A correlated
    subquery per timestamp needs no grouping at all.
    """
    from cold_outreach.emails.models import Direction, Message
    from cold_outreach.emails.models.maillog import TURN_KINDS

    def newest(direction: str) -> Subquery:
        return Subquery(
            Message.objects
            .filter(thread=OuterRef("thread_id"), direction=direction, kind__in=TURN_KINDS)
            .order_by("-sent_at")
            .values("sent_at")[:1]
        )

    return (
        Deal.objects.filter(
            campaign=campaign,
            state=DealState.EMAILED,
            outcome="",
            thread__isnull=False,
        )
        .annotate(last_in=newest(Direction.INBOUND), last_out=newest(Direction.OUTBOUND))
        .filter(last_in__isnull=False)
        .filter(Q(last_out__isnull=True) | Q(last_in__gt=F("last_out")))
        .select_related("lead", "campaign", "mailbox")
        .order_by("last_in")
    )
