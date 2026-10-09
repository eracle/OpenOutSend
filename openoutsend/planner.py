"""The planner: what is due, from which box, and when next.

Every decision about *sending* lives here, and nothing else does. A send pass
(`send_pass.py`) asks what is due and which box carries it, then writes and sends; a
bounded run (`send_job.py`) asks when the next first email could go and sleeps on that
one answer. Neither holds a rule of its own about who is owed an email or which box
may carry it.

**Everything is derived from state that already exists** — a deal's own state, the mail
log's timestamps, and each box's ledger and spacing clock. Nothing is minted, no queue
table is written, and no row's timestamp gates another's, which is what lets a pass be
interrupted anywhere and simply asked again.

The follow-up pool is the one most tempting to build the other way, and the reason not
to is on the record: the version this replaces kept a `next_follow_up_at` on every deal
which the agent re-armed itself and then failed to push out, so one deal's unchanged
context was re-read five times inside a single window.

**The rules the planner keeps:**

- **One budget per box.** Every first email and every follow-up counts against its
  box's headroom, spacing and window. Only a reply to someone who wrote in is exempt:
  it is not cold volume, and answering within minutes is more human, not less.
- **Follow-ups before openers, and that is not a priority.** Both draw from the same
  per-box headroom and the same spacing clock, so whichever goes first simply takes the
  slot; follow-ups first means a box's last send of the day goes to a thread already
  started rather than opening one more it will never return to. The old arrangement —
  follow-ups claiming *ahead of* openers out of a separate budget — is what produced
  102 follow-ups against 1 first email in a week, and it is unreachable here: there is
  one budget.
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

from django.db.models import Count, F, OuterRef, Q, Subquery
from django.utils import timezone

# ── What is due ───────────────────────────────────────────────────


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
    from openoutsend.leads.models import Deal, DealState

    return (
        Deal.objects.filter(state=DealState.READY)
        .exclude(lead__email="")
        .select_related("lead")
        .order_by("created_at")
    )


def unanswered_replies():
    """`EMAILED` deals whose newest inbound turn is newer than our newest outgoing one.

    **This is the entire reply trigger.** No timer, no flag, no bookkeeping: the mail
    pass writes inbound rows, and the comparison between two timestamps says whether the
    ball is in our court. Oldest reply first, so nobody waits behind a livelier thread.

    **Turns, not messages.** A bounce and an out-of-office sit in the thread and are
    not somebody writing back, so neither can make a deal actionable — which is what
    stops an agent apologising twice to a dead address.

    **The two timestamps are subqueries, not aggregates.** `Max()` over the joined
    messages reads the same and turns the query into a `GROUP BY` over every selected
    column — including everything `select_related` just widened it with. A correlated
    subquery per timestamp needs no grouping at all.
    """
    from openoutsend.emails.models import Direction

    return (
        _open_threads()
        .annotate(last_in=_newest_turn(Direction.INBOUND),
                  last_out=_newest_turn(Direction.OUTBOUND))
        .filter(last_in__isnull=False)
        .filter(Q(last_out__isnull=True) | Q(last_in__gt=F("last_out")))
        .select_related("lead", "mailbox")
        .order_by("last_in")
    )


def awaiting_follow_up(now=None):
    """`EMAILED` deals nobody answered, due a follow-up. Longest-waiting first.

    **The mirror image of `unanswered_replies`**: same two timestamps, the opposite
    comparison. There, their newest turn is newer than ours and the ball is in our
    court. Here nothing of theirs has arrived at all since we wrote, so what decides
    is how long ago that was and how many times we have already asked.

    Nothing at all when the install has follow-ups off (``OUTSEND_FOLLOW_UPS``). Else
    four conditions, and none of them is a column:

    - **Nothing inbound, ever.** A reply — even one already answered — takes the deal
      out of this pool for good, because `last_in` then exists. A bounce or an
      out-of-office is *not* a turn, so neither can rescue a lead from being chased,
      which is deliberate: an auto-reply is not somebody saying anything.
    - **Fewer follow-ups since the last step than there are gaps.**
    - **The gap for the follow-up we are about to send has elapsed**, in business days.
    - **It lands before the next step is due** (`_fits_before_next_step`).

    The last three **cannot be SQL** — business days are not an interval any database
    computes, and the next step is on disk — so the query narrows on the calendar-day
    floor of the smallest gap and the real arithmetic runs in Python over what
    survives. The floor is a lower bound by construction (a business day is never
    shorter than a calendar day), so nothing due is ever excluded by it.

    Returns a list rather than a queryset, since the last filters are not expressible.
    """
    from openoutsend.core.conf import FOLLOW_UP_GAPS_BUSINESS_DAYS, follow_ups_on
    from openoutsend.emails.models import Direction

    if not follow_ups_on():
        return []
    now = now or timezone.now()
    candidates = (
        _open_threads()
        .annotate(last_in=_newest_turn(Direction.INBOUND),
                  last_out=_newest_turn(Direction.OUTBOUND))
        .filter(last_in__isnull=True, last_out__isnull=False, step__isnull=False)
        .filter(last_out__lte=now - timedelta(days=min(FOLLOW_UP_GAPS_BUSINESS_DAYS)))
        .select_related("lead", "mailbox")
        .order_by("last_out")
    )
    sequence_of = _sequences()
    return [deal for deal in candidates if _follow_up_is_due(deal, sequence_of(deal), now)]


def exhausted_touches():
    """`EMAILED` deals past their last step and its follow-ups, who answered none of it.

    The end of the pursuit. Separate from `awaiting_follow_up` because closing one
    costs no send: it is not gated on a mailbox, a window or a daily cap, so a run
    outside working hours still tidies up what it cannot write to.

    With follow-ups off, that is the moment the last step has gone out; with them on,
    the moment the last follow-up has. The query narrows on the one count that is a
    lower bound either way — the last step plus its follow-ups — and the step is
    checked in Python, because the sequence is on disk.

    Silence, not refusal — see `emails/steps/follow_up.give_up` for why the address
    is not suppressed.
    """
    from openoutsend.emails.models import Direction

    follow_ups = _follow_ups_allowed()
    candidates = (
        _open_threads()
        .annotate(last_in=_newest_turn(Direction.INBOUND), touches=_outbound_turns())
        .filter(last_in__isnull=True, step__isnull=False, touches__gt=follow_ups)
        .select_related("lead")
        .order_by("created_at")
    )
    sequence_of = _sequences()
    return [
        deal for deal in candidates
        if sequence_of(deal).after(deal.step) is None
        and _since_last_step(deal)[1] >= follow_ups
    ]


def _open_threads():
    """`EMAILED` deals with a thread and no outcome yet — every pool after the opener."""
    from openoutsend.leads.models import Deal, DealState

    return Deal.objects.filter(state=DealState.EMAILED, outcome="", thread__isnull=False)


def _newest_turn(direction: str) -> Subquery:
    """When the deal's thread last carried a turn in *direction* — bounces excluded."""
    from openoutsend.emails.models import Message
    from openoutsend.emails.models.maillog import TURN_KINDS

    return Subquery(
        Message.objects
        .filter(thread=OuterRef("thread_id"), direction=direction, kind__in=TURN_KINDS)
        .order_by("-sent_at")
        .values("sent_at")[:1]
    )


def _outbound_turns() -> Count:
    """How many times we have written in the deal's thread — the touches so far."""
    from openoutsend.emails.models import Direction
    from openoutsend.emails.models.maillog import TURN_KINDS

    return Count(
        "thread__messages",
        filter=Q(thread__messages__direction=Direction.OUTBOUND,
                 thread__messages__kind__in=TURN_KINDS),
        distinct=True,
    )


def _follow_up_is_due(deal, sequence, now) -> bool:
    """Whether *deal*'s next follow-up has waited its gap and still fits."""
    from openoutsend.core.business_time import business_days_between
    from openoutsend.core.conf import FOLLOW_UP_GAPS_BUSINESS_DAYS

    step_sent_at, follow_ups = _since_last_step(deal)
    if follow_ups >= len(FOLLOW_UP_GAPS_BUSINESS_DAYS):
        return False
    # The gap that applies is the one before the follow-up we are about to send,
    # counted from our last send, so the first waits the first gap after the step.
    if business_days_between(deal.last_out, now) < FOLLOW_UP_GAPS_BUSINESS_DAYS[follow_ups]:
        return False
    return _fits_before_next_step(sequence.after(deal.step), step_sent_at, now)


def _fits_before_next_step(next_step, step_sent_at, now) -> bool:
    """Whether a follow-up sent now lands before the next step is due.

    The next step counts from the previous *step*, never from a follow-up, so fitting
    them in never pushes a sequence later; one that does not fit is skipped, not
    queued. After the last step there is nothing to land before, so every one fits.
    """
    from openoutsend.core.business_time import business_days_between

    if next_step is None:
        return True
    return business_days_between(step_sent_at, now) < next_step.days


def _since_last_step(deal) -> tuple:
    """When *deal*'s last step went out, and how many of our sends followed it.

    Read off the mail log, where every step send carries its number: the sends after
    the newest one are the follow-ups to it, since a deal with a reply in it is in
    neither pool that asks.
    """
    from openoutsend.emails.models import Direction

    sends = list(deal.thread.turns().filter(direction=Direction.OUTBOUND)
                 .values_list("step", "sent_at"))
    for index in range(len(sends) - 1, -1, -1):
        step, sent_at = sends[index]
        if step is not None:
            return sent_at, len(sends) - 1 - index
    return None, len(sends)


def _follow_ups_allowed() -> int:
    """How many follow-ups a step gets at most: the gaps, or none with them off."""
    from openoutsend.core.conf import FOLLOW_UP_GAPS_BUSINESS_DAYS, follow_ups_on

    return len(FOLLOW_UP_GAPS_BUSINESS_DAYS) if follow_ups_on() else 0


def _sequences():
    """A lookup from deal to its sequence that reads each folder once per call."""
    from openoutsend.core.sequences import for_deal

    loaded: dict = {}

    def sequence_of(deal):
        if deal.sequence not in loaded:
            loaded[deal.sequence] = for_deal(deal)
        return loaded[deal.sequence]

    return sequence_of


# ── From which box ────────────────────────────────────────────────


def free_for_first_email():
    """The box that may send a first email right now, or None.

    The clock speaks first and for the whole pool: outside the operator's working
    window no box may open a conversation, however much headroom it has
    (``core/sending_window.py``). Then three conditions, all per box: headroom left
    today, not paused by the receiver (both inside ``headroom_today``), and its spacing
    clock elapsed. Among the free boxes the most idle one wins, so volume spreads rather
    than piling on whichever row sorts first.

    Only *first* emails come through here. A reply is not cold volume and is sent
    regardless of window, cap or spacing. A **follow-up is** cold volume, but it cannot
    pick its box — the thread already has one — so ``follow_ups_due`` asks that box
    directly.
    """
    from openoutsend.core.sending_window import within_sending_window
    from openoutsend.emails.models import Mailbox

    if not within_sending_window():
        return None
    now = timezone.now()
    ranked = [
        (box, headroom) for box in Mailbox.objects.all()
        if box.free_now(now) and (headroom := box.headroom_today()) > 0
    ]
    if not ranked:
        return None
    return max(ranked, key=lambda pair: pair[1])[0]


def first_emails_due() -> Iterator[tuple]:
    """``(deal, box)`` for each first email that may go now — oldest lead, freest box.

    The bound is the guards themselves: ``free_for_first_email`` answers ``None``
    outside the window, for a box at its ceiling, and for one whose spacing clock has
    not elapsed — and a send rewrites that clock, so a box takes itself out of the pool
    on its way past. The box is asked again for every pair, **after** the caller has
    sent the previous one, which is why this is a generator and not a list.
    """
    waiting = emailable_deals().iterator()
    while (box := free_for_first_email()) is not None:
        deal = next(waiting, None)
        if deal is None:
            return
        yield deal, box


def follow_ups_due(now=None) -> Iterator:
    """Each deal whose follow-up may go now, longest-waiting first.

    The box is not chosen: it is the thread's own (``deal.mailbox``), because answering
    from another would break the conversation. **A box that is not free is skipped, not
    waited for**, so a busy box means its threads wait for the next pass while the
    others continue. Outside the sending window nothing is due at all.
    """
    from openoutsend.core.sending_window import within_sending_window

    if not within_sending_window():
        return
    for deal in awaiting_follow_up(now):
        box = deal.mailbox
        if box is None or not box.free_now() or box.headroom_today() <= 0:
            continue
        yield deal


# ── When next ─────────────────────────────────────────────────────


def next_first_email_at(now=None):
    """When the pool could next open a conversation, or ``None`` if it never could.

    ``free_for_first_email`` says *not now* and cannot say *why* or *for how long* —
    which is all a bounded run needs to know before it decides to wait
    (``send_job.py``). The same three gates, read as a clock instead of a boolean:

        spacing   the box's own ``next_send_at``
        window    the next 08:00 at or after that
        cap       a box out of headroom is out until the ledger rolls, so it is
                  asked about tomorrow morning rather than about today

    The soonest box wins, because one free box is all a send needs. ``None`` means no
    mailbox is connected — the one state no amount of waiting resolves. A drained *lead*
    pool is not this function's business: it is about the boxes.
    """
    from openoutsend.core.sending_window import next_window_open
    from openoutsend.emails.models import Mailbox
    from openoutsend.emails.models.mailbox import _local_midnight

    now = now or timezone.now()
    tomorrow = _local_midnight() + timedelta(days=1)
    return min(
        (
            next_window_open(max(now, box.next_send_at or now))
            if box.headroom_today() > 0 else next_window_open(tomorrow)
            for box in Mailbox.objects.all()
        ),
        default=None,
    )
