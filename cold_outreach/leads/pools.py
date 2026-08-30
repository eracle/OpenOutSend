"""The three questions a send pass asks the database: who is waiting, who wrote back,
and who has gone quiet long enough to be worth one more email.

All three are **derived from state that already exists** — a deal's own state and the
mail log's timestamps. Nothing is minted, no queue table is written, and no row's
timestamp gates another's, which is what lets a pass be interrupted anywhere and simply
asked again.

The follow-up pool is the one most tempting to build the other way, and the reason not
to is on the record: the version this replaces kept a `next_follow_up_at` on every deal
which the agent re-armed itself and then failed to push out, so one deal's unchanged
context was re-read five times inside a single window.
"""
from __future__ import annotations

from django.db.models import Count, F, OuterRef, Q, Subquery

from cold_outreach.leads.models import Deal, DealState


def emailable_deals():
    """Deals waiting for their first email, oldest first.

    Two conditions and no more: the state and an address to send to.
    **Suppression is not a third filter** — an opt-out moves every open deal for that
    address to `UNSUBSCRIBED` when it lands, and ingest parks a suppressed row there on
    arrival, so the state already carries the answer. `emails/sender.suppressed` asks
    the list again after the agent has written, which is the check that catches an
    opt-out arriving in the seconds an LLM call takes.

    Oldest first, so a lead who has been waiting a week is not overtaken by one
    ingested this morning.
    """
    return (
        Deal.objects.filter(state=DealState.READY)
        .exclude(lead__email="")
        .select_related("lead")
        .order_by("created_at")
    )


def unanswered_replies():
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
            state=DealState.EMAILED,
            outcome="",
            thread__isnull=False,
        )
        .annotate(last_in=newest(Direction.INBOUND), last_out=newest(Direction.OUTBOUND))
        .filter(last_in__isnull=False)
        .filter(Q(last_out__isnull=True) | Q(last_in__gt=F("last_out")))
        .select_related("lead", "mailbox")
        .order_by("last_in")
    )


def awaiting_follow_up(now=None):
    """`EMAILED` deals nobody answered, due another touch. Longest-waiting first.

    **The mirror image of `unanswered_replies`**: same two timestamps, the opposite
    comparison. There, their newest turn is newer than ours and the ball is in our
    court. Here nothing of theirs has arrived at all since we wrote, so what decides
    is how long ago that was and how many times we have already asked.

    Three conditions, and none of them is a column:

    - **Nothing inbound since our last outbound.** A reply — even one already
      answered — takes the deal out of this pool for good, because `last_in` then
      exists and is newer. A bounce or an out-of-office is *not* a turn, so neither
      can rescue a lead from being chased, which is deliberate: an auto-reply is not
      somebody saying anything.
    - **Fewer than `MAX_COLD_TOUCHES` outbound turns.** The cap that ends the pursuit.
    - **The gap for the touch we are about to send has elapsed**, in business days.

    That last one **cannot be SQL** — business days are not an interval any database
    computes — so the query narrows on the calendar-day floor of the smallest gap and
    the real arithmetic runs in Python over what survives. The floor is a lower bound
    by construction (a business day is never shorter than a calendar day), so nothing
    due is ever excluded by it.

    Returns a list rather than a queryset, since the last filter is not expressible.
    """
    from datetime import timedelta

    from django.utils import timezone

    from cold_outreach.core.conf import FOLLOW_UP_GAPS_BUSINESS_DAYS, MAX_COLD_TOUCHES
    from cold_outreach.emails.models import Direction, Message
    from cold_outreach.emails.models.maillog import TURN_KINDS

    now = now or timezone.now()

    def newest(direction: str) -> Subquery:
        return Subquery(
            Message.objects
            .filter(thread=OuterRef("thread_id"), direction=direction, kind__in=TURN_KINDS)
            .order_by("-sent_at")
            .values("sent_at")[:1]
        )

    candidates = (
        Deal.objects.filter(
            state=DealState.EMAILED,
            outcome="",
            thread__isnull=False,
        )
        .annotate(
            last_in=newest(Direction.INBOUND),
            last_out=newest(Direction.OUTBOUND),
            touches=Count(
                "thread__messages",
                filter=Q(thread__messages__direction=Direction.OUTBOUND,
                         thread__messages__kind__in=TURN_KINDS),
                distinct=True,
            ),
        )
        .filter(last_in__isnull=True, last_out__isnull=False)
        .filter(touches__lt=MAX_COLD_TOUCHES, touches__gt=0)
        .filter(last_out__lte=now - timedelta(days=min(FOLLOW_UP_GAPS_BUSINESS_DAYS)))
        .select_related("lead", "mailbox")
        .order_by("last_out")
    )
    return [deal for deal in candidates if _follow_up_is_due(deal, now)]


def exhausted_touches():
    """`EMAILED` deals that have had every touch and answered none of them.

    The end of the pursuit. Separate from `awaiting_follow_up` because closing one
    costs no send: it is not gated on a mailbox, a window or a daily cap, so a run
    outside working hours still tidies up what it cannot write to.

    Silence, not refusal — see `emails/steps/follow_up.give_up` for why the address
    is not suppressed.
    """
    from cold_outreach.core.conf import MAX_COLD_TOUCHES
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
            state=DealState.EMAILED,
            outcome="",
            thread__isnull=False,
        )
        .annotate(
            last_in=newest(Direction.INBOUND),
            touches=Count(
                "thread__messages",
                filter=Q(thread__messages__direction=Direction.OUTBOUND,
                         thread__messages__kind__in=TURN_KINDS),
                distinct=True,
            ),
        )
        .filter(last_in__isnull=True, touches__gte=MAX_COLD_TOUCHES)
        .select_related("lead")
        .order_by("created_at")
    )


def _follow_up_is_due(deal, now) -> bool:
    """Whether *deal*'s next touch has waited its gap, in working days."""
    from cold_outreach.core.business_time import business_days_between
    from cold_outreach.core.conf import FOLLOW_UP_GAPS_BUSINESS_DAYS

    # `touches` counts what has gone out; the gap that applies is the one before the
    # touch we are about to send, so a deal with one send waits the first gap.
    gap = FOLLOW_UP_GAPS_BUSINESS_DAYS[deal.touches - 1]
    return business_days_between(deal.last_out, now) >= gap
