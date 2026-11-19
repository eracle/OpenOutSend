"""The planner: the send guards. Which box may carry a send that is due, and when next.

**Which email a deal is owed is its state, and when it is due is that state's wait**
(`leads/lifecycle.py`). A deal whose step is due rests in `sending_E<k>`, and this
module answers the one question left: may it go now, and from which box. A send pass
(`send_pass.py`) asks that and sends; a bounded run (`send_job.py`) asks when the next
first email could go and sleeps on the answer.

**Everything is derived from state that already exists**: a deal's state and since when
it has rested there, and each box's ledger and spacing clock. No queue table is written,
which is what lets a pass be interrupted anywhere and simply asked again.

**The rules the planner keeps:**

- **A pass sends every sequence in the install**, cold among them, in one queue, because
  they all spend the same boxes. ``sequence`` (a ``core/sequences.key``) narrows it to
  one, which is what `outsend send --sequence F` passes.
- **One budget per box.** Every step of every sequence waits for its box's spacing
  clock and its lead's window, and needs headroom left on the box; the headroom itself
  is spent by first emails, counted as threads begun today (``Mailbox.sent_today``).
  Only an answer to someone who wrote in is exempt: it is not cold volume, and
  answering within minutes is more human, not less. A resend box
  is the one exception, for headroom only: its cap is a budget the run declares, so
  each sequence spends its own (``Mailbox.sent_today``), and an old backlog in one
  cannot starve another. Spacing and window stay the box's.
- **A deal's first send picks the freest box; every later one stays on the thread's.**
- **Oldest due first** (`sends_due`): one queue ordered by how long each deal has
  rested in its `sending` state, so a first email and a later step cannot starve one
  another.
- **At most one message per address per pass.** A lead in two sequences is two deals,
  and a lead whose reply this pass answered is not sent a step besides it.
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

from django.utils import timezone

from openoutsend.leads.states import CONVERSING, SENDING

# ── What is due ───────────────────────────────────────────────────


def due_deals(sequence: str | None = None) -> list:
    """The deals whose step is due, longest-due first, each with its ``step``.

    Due is the state: a deal in a `sending` state of its sequence's machine. One with
    no address is not here: it rests until an enrichment fills the address in.
    **Suppression is not a filter**: an opt-out ends every open deal of its address
    when it lands, so the state already carries the answer, and
    `emails/sender.suppressed` asks the list again after the email is written.
    """
    from openoutsend.leads.models import Deal

    waiting = (
        _scoped(Deal.objects.open(), sequence)
        .with_state()
        .exclude(lead__email="")
        .select_related("lead", "mailbox", "thread")
        .order_by("entered_at", "pk")
    )
    machine_of = machines()
    due = []
    for deal in waiting:
        state = _state_of(deal, machine_of(deal))
        if SENDING in state.tags:
            deal.due_step = state.handler.file
            due.append(deal)
    return due


def _state_of(deal, machine):
    """The state *deal* rests in, as its machine declares it.

    A folder edited while deals are open can leave one in a state it no longer has (a
    step removed or renumbered). That deal is named, never guessed at.
    """
    from openoutsend.core.sequences import SequenceError

    try:
        return machine.states(deal.current_state)
    except ValueError:
        raise SequenceError(
            f"deal {deal.pk} ({deal.lead.public_id}) rests in {deal.current_state!r}, a state "
            f"the sequence {deal.sequence!r} no longer has: restore the step, or move the deal"
        ) from None


def waiting_to_open(sequence: str | None = None):
    """Open deals not yet written to that have an address: who a first email is still owed."""
    from openoutsend.leads.models import Deal

    return (
        _scoped(Deal.objects.open(), sequence)
        .filter(thread__isnull=True)
        .exclude(lead__email="")
        .select_related("lead")
    )


def conversations_waiting() -> list:
    """Each deal in its conversation with a message waiting for an answer, oldest message first.

    Never narrowed to a sequence: whoever wrote in is answered in whichever pass reads
    the mail.
    """
    from openoutsend.leads.models import Deal

    machine_of = machines()
    waiting = []
    for deal in Deal.objects.in_state(CONVERSING).select_related("lead", "mailbox", "thread"):
        message = machine_of(deal).incoming(deal)
        if message is not None:
            waiting.append((message.pk, deal))
    return [deal for _, deal in sorted(waiting, key=lambda pair: pair[0])]


def _scoped(deals, sequence: str | None):
    """*deals* in *sequence* alone, or all of them when no sequence is named."""
    return deals if sequence is None else deals.filter(sequence=sequence)


def _zone_of(deal):
    """The zone *deal*'s lead reads the clock in — theirs, or the operator's when blank."""
    from openoutsend.core.sending_window import lead_timezone

    return lead_timezone(deal.lead)


def machines():
    """A lookup from deal to its machine that reads each sequence folder once."""
    from openoutsend.leads.lifecycle import machine_of

    loaded: dict = {}

    def of(deal):
        if deal.sequence not in loaded:
            loaded[deal.sequence] = machine_of(deal)
        return loaded[deal.sequence]

    return of


# ── From which box ────────────────────────────────────────────────


def free_for_first_email(sequence: str | None = None, zone=None):
    """The box that may send a first email for *sequence* to a lead in *zone* right now, or None.

    The clock speaks first and for the whole pool: outside the lead's working window
    (*zone*, the operator's when left out) no box may open a conversation with them,
    however much headroom it has (``core/sending_window.py``). Then three conditions,
    all per box: headroom left
    today, not paused by the receiver (both inside ``headroom_today``), and its spacing
    clock elapsed. Among the free boxes the most idle one wins, so volume spreads rather
    than piling on whichever row sorts first.

    Only *first* sends come through here. A later step is cold volume too, but it
    cannot pick its box — the thread already has one — so ``free_thread_box`` asks that
    box directly. An answer in a conversation is sent regardless of window, cap or
    spacing.
    """
    from openoutsend.core.sending_window import within_sending_window
    from openoutsend.emails.models import Mailbox

    if not within_sending_window(zone=zone):
        return None
    now = timezone.now()
    ranked = [
        (box, headroom) for box in Mailbox.objects.all()
        if box.free_now(now) and (headroom := box.headroom_today(sequence)) > 0
    ]
    if not ranked:
        return None
    return max(ranked, key=lambda pair: pair[1])[0]


def free_thread_box(deal):
    """The box *deal*'s thread lives on, if it may send cold volume right now — else None.

    The same gates as ``free_for_first_email``, on the one box the thread allows. The
    spacing clock is re-read, because the queue is built before the loop that sends: a
    send earlier in the loop moved the clock on a different copy of the box.
    """
    from openoutsend.core.sending_window import within_sending_window

    box = deal.mailbox
    if box is None or not within_sending_window(zone=_zone_of(deal)):
        return None
    box.refresh_from_db(fields=["next_send_at"])
    return box if box.free_now() and box.headroom_today(deal.sequence) > 0 else None


def free_box(deal):
    """The box that may carry *deal*'s due step right now, or ``None``."""
    if deal.thread_id is None:
        return free_for_first_email(deal.sequence, _zone_of(deal))
    return free_thread_box(deal)


def plan_for(deal):
    """What the guards choose for *deal*'s due step right now, or ``None`` when no box is free.

    For a send fired with no pass around it (``agfsm``, ``progress``): the box the pass
    would have picked, and a prompt line drawn as the pass draws one.
    """
    from openoutsend.core.prompt_lines import choose
    from openoutsend.emails.steps.send import Plan

    if not deal.lead.email:
        return None
    box = free_box(deal)
    return Plan(box, prompt_line=choose(None)) if box is not None else None


def sends_due(*, sequence: str | None = None, agent_drafted: bool = False,
              reached: set | None = None) -> Iterator[tuple]:
    """``(deal, box)`` for each due step that may go now — longest due first.

    **The box:** a deal's first send takes the freest box in the install
    (``free_for_first_email``); every later one stays on its thread's box, because
    writing from another would break the conversation. **A send whose box is not free
    is skipped, not waited for**, so one busy box holds its own threads to the next
    pass while the rest continue. **The window is each lead's own**: a send whose lead
    is outside 08:00–20:00 where they are is skipped, and the leads whose day it is go
    meanwhile.

    The box is asked for each send **after** the caller has sent the previous one — a
    send rewrites its box's spacing clock — which is why this is a generator. With
    ``agent_drafted`` the steps written from a prompt are left out, for a pass whose
    prompts are answered by a calling agent instead (``send_pass._send_agent_draft``).
    An email step has nothing to draft and stays in.

    **At most one message per address per pass.** A lead in two sequences is two deals,
    and both can fall due together; the older goes and the other waits for the next
    pass. ``reached`` is the set of lead ids already written to in this pass — the
    conversations answered — and every send yielded here joins it.
    """
    reached = set() if reached is None else reached
    pool_closed = set()
    for deal in due_deals(sequence):
        if deal.lead_id in reached or (agent_drafted and not deal.due_step.is_email):
            continue
        if deal.thread_id is not None:
            box = free_thread_box(deal)
        elif (pool := (deal.sequence, _zone_of(deal))) not in pool_closed:
            # A pass only ever uses up boxes, and a window does not open mid-pass, so a
            # pool with none free for a sequence and a zone stays that way for both.
            box = free_for_first_email(*pool)
            if box is None:
                pool_closed.add(pool)
        else:
            continue
        if box is not None:
            reached.add(deal.lead_id)
            yield deal, box


# ── When next ─────────────────────────────────────────────────────


def next_first_email_at(now=None, sequence: str | None = None):
    """When the pool could next open a conversation, or ``None`` if it never could.

    ``free_for_first_email`` says *not now* and cannot say *why* or *for how long* —
    which is all a bounded run needs to know before it decides to wait
    (``send_job.py``). The same three gates, read as a clock instead of a boolean:

        spacing   the box's own ``next_send_at``
        window    the next 08:00 at or after that, in the lead's zone
        cap       a box out of headroom is out until the ledger rolls, so it is
                  asked about tomorrow morning rather than about today

    And one more, from the leads' side: **no first email before the earliest is due**.
    A deal enrolled for ``E0-2d`` is not worth waking for today, however free the boxes.
    Leads in different zones open at different hours, so the earliest due first email
    of each zone is asked, and the soonest of them wins.

    The soonest box wins, because one free box is all a send needs. ``None`` means no
    mailbox is connected — the one state no amount of waiting resolves. A drained *lead*
    pool is not this function's business: with nobody waiting it answers for the boxes,
    on the operator's clock.
    """
    from openoutsend.core.sending_window import next_window_open
    from openoutsend.emails.models import Mailbox
    from openoutsend.emails.models.mailbox import _local_midnight

    now = now or timezone.now()
    tomorrow = _local_midnight() + timedelta(days=1)
    boxes = [(box.next_send_at, box.headroom_today(sequence) > 0) for box in Mailbox.objects.all()]
    return min(
        (
            next_window_open(max(earliest, next_send_at or earliest) if has_headroom
                             else max(earliest, tomorrow), zone)
            for zone, earliest in _earliest_first_email_by_zone(now, sequence).items()
            for next_send_at, has_headroom in boxes
        ),
        default=None,
    )


def _earliest_first_email_by_zone(now, sequence: str | None) -> dict:
    """``{zone: when its earliest first email is due}``, never before *now*.

    A deal already in a `sending` state is due now; one still waiting is due when its
    wait runs out. The operator's zone at *now* when nobody is waiting, so the boxes
    are still asked.
    """
    from openoutsend.core.sending_window import operator_timezone
    from openoutsend.core.sequences import named
    from openoutsend.leads.lifecycle import opening_step

    earliest: dict = {}
    sequences: dict = {}
    for deal in waiting_to_open(sequence):
        if deal.sequence not in sequences:
            sequences[deal.sequence] = named(deal.sequence)
        opening = opening_step(deal, sequences[deal.sequence])
        if opening is None:
            continue
        zone, due_at = _zone_of(deal), max(now, opening[1])
        if zone not in earliest or due_at < earliest[zone]:
            earliest[zone] = due_at
    return earliest or {operator_timezone(): now}
