"""A bounded run: open N conversations, however long the clocks take.

`outsend send 5` says *five new conversations*, and this reaches that goal the way
`openoutreach find 5` reaches its own — by working until the number is met or until
nothing can produce another one. The difference is what stands in the way. The finder
is blocked by *work*, so when nothing can advance it stops and there is nothing to wait
for. The sender is blocked by *clocks*: a box that just opened one is three to four
minutes from opening the next, a pool at its ceiling is a day from opening any, and a
Friday evening is Monday morning. Those all resolve by themselves, so this waits.

**The state machine already knows when.** Nothing here re-implements a guard: it asks
the pool the same three questions in clock form (``Mailbox.objects.next_first_email_at``
— spacing, window, daily cap) and sleeps until the answer. That is what makes an
external cycler unnecessary rather than replaced — the thing it was polling for is a
timestamp this side can read directly.

**The wait is spent working.** Every slice ends in a whole pass, so the mail is read and
replies are answered on a five-minute cadence throughout (``conf.WAIT_SLICE_SECONDS``);
only the openers are on hold. Follow-ups and replies do not count toward the goal — the
goal is cold volume, and answering somebody who wrote to you is not that.

**What ends a run that is not finished:**

    drained     nobody left to email — the lead pool is the one thing waiting cannot
                refill, since ingest is a separate invocation. This is the one ending
                `send all` is *asking* for, and it ends that run successfully
    no box      no mailbox connected, which no clock resolves
    refused     two passes in a row that failed a send and opened nothing — a receiver
                saying no, as opposed to the blip a single failure might be
    Ctrl-C      the operator's own deadline; the conversations already opened stand

`outsend send` with no count stays exactly one pass, because a timer wants a pass and
not a resident process.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime

from cold_outreach.core.conf import WAIT_SLICE_SECONDS
from cold_outreach.send_pass import PassResult, run_send_pass

logger = logging.getLogger(__name__)

# Consecutive passes that fail a send and open nothing before the run gives up. One
# failure is a blip — a dropped connection, a 4xx deferral — and the next pass is the
# retry, which is why it is not one. Two in a row is the receiver's answer, and waiting
# out a clock to hear it again is how a bounded run becomes the daemon it replaced.
REFUSALS_BEFORE_GIVING_UP = 2

DRAINED = "drained"
NO_MAILBOX = "no_mailbox"
REFUSED = "refused"
INTERRUPTED = "interrupted"

ALL = "all"
"""The goal that is the pool itself: keep opening until nobody is left to email.

**It is the one goal that inverts the verdict on ``DRAINED``.** For `send 5`, an empty
pool is failure — five were asked for and three arrived. For `send all`, an empty pool
is the whole point, so the same stop is success. Nothing else changes: it waits out the
same clocks, and a missing mailbox or a refusing receiver still ends the run badly.

Spelled as a word rather than as a number the operator has to look up first, because
the count that means "everything" goes stale between reading it and typing it.
"""


@dataclass
class SendJobResult:
    """What the run did, summed over its passes — what the exit code is built from."""

    goal: int | str
    """A count, or ``ALL``."""

    passes: int = 0
    totals: PassResult = field(default_factory=PassResult)
    stopped_because: str | None = None
    """``None`` when the goal was met; otherwise why the run stopped short."""

    detail: str = ""
    elapsed: float = 0.0
    """Wall-clock seconds, waiting included. Reported, never enforced."""

    @property
    def opened(self) -> int:
        """Conversations opened — the only thing the goal counts."""
        return self.totals.opened

    @property
    def drained_the_pool(self) -> bool:
        """Whether the run ended because nobody was left to email."""
        return self.stopped_because == DRAINED

    @property
    def reached(self) -> bool:
        """Whether the run did what was asked — which depends on what was asked.

        ``send all`` asks for the pool, so emptying it *is* the goal; ``send 5`` asks
        for five, and an empty pool at three is the run falling short.
        """
        if self.goal == ALL:
            return self.stopped_because in (None, DRAINED)
        return self.stopped_because is None

    @property
    def ok(self) -> bool:
        """True when the goal was met and nothing failed on its way out."""
        return self.reached and self.totals.ok

    @property
    def progress(self) -> str:
        """How far the run got, phrased the way the goal was asked for."""
        return f"{self.opened} opened" if self.goal == ALL else f"{self.opened} of {self.goal}"


def run_send_job(goal: int | str, prompt_line_name: str | None = None,
                 sleep=time.sleep) -> SendJobResult:
    """Open *goal* conversations, waiting out the clocks in between.

    ``goal`` is a count, or ``ALL`` to keep going until the pool is empty.

    ``sleep`` is injected so a test can run a week of waiting in milliseconds. It is the
    only clock this module holds: every *duration* comes from the pool's own timestamps.
    """
    started = time.monotonic()
    result = _work_to_goal(goal, prompt_line_name, sleep)
    result.elapsed = time.monotonic() - started
    return result


def _work_to_goal(goal: int | str, prompt_line_name: str | None, sleep) -> SendJobResult:
    """The loop itself. Every exit is a ``SendJobResult``; none of them raises."""
    from django.utils import timezone

    result = SendJobResult(goal=goal)
    # ``ALL`` is a target no count can reach, which is the honest way to say it: the run
    # ends on the pool being empty, never on the arithmetic.
    target = math.inf if goal == ALL else goal
    refusals = 0
    announced = ""

    while True:
        try:
            done = _run_one_pass(prompt_line_name, result)
        except KeyboardInterrupt:
            return _interrupted(result)

        if result.opened >= target:
            return result

        refusals = refusals + 1 if done.failed and not done.opened else 0
        if refusals >= REFUSALS_BEFORE_GIVING_UP:
            return _stop(result, REFUSED,
                         "the last two passes failed a send and opened nothing — "
                         "the box or the receiver is refusing, and waiting will not change it")

        opening_at = _next_opening()
        if opening_at is None:
            return _stop(result, NO_MAILBOX,
                         "no mailbox connected, so nothing can be sent — run `outsend init`")
        if not _anyone_waiting():
            return _stop(result, DRAINED,
                         "nobody left to email — pipe in more leads with "
                         "`openoutreach find N --json | outsend`")

        announced = _announce(result, opening_at, announced, done)
        try:
            sleep(_wait_before_the_next_pass(opening_at, timezone.now(), idle=not done.opened))
        except KeyboardInterrupt:
            return _interrupted(result)


# ── One pass ──────────────────────────────────────────────────────


def _run_one_pass(prompt_line_name: str | None, result: SendJobResult) -> PassResult:
    """Run a pass and fold its counts into the run's totals. Returns the pass's own."""
    done = run_send_pass(prompt_line_name, narrate=False)
    result.passes += 1
    for count in ("mirrored", "classified", "projected", "answered",
                  "opened", "followed_up", "gave_up", "failed"):
        setattr(result.totals, count, getattr(result.totals, count) + getattr(done, count))
    return done


# ── The wait ──────────────────────────────────────────────────────


def _wait_before_the_next_pass(opening_at: datetime, now: datetime, idle: bool) -> float:
    """Seconds to sleep before the next pass — the wait, capped at one slice.

    Capped rather than slept in one go so the mail keeps being read while the openers
    are on hold, and so an interrupt is noticed in minutes rather than in hours.

    **An idle pass always waits a whole slice**, whatever the clock says. A wait of zero
    can only follow a pass that opened nothing while the pool said a box was free — the
    guards said yes and the pass disagreed — and asking again in the same instant is a
    spin, not a retry. The cost when the disagreement is benign (a follow-up took the
    box's slot) is one slice against a four-minute clock.
    """
    if idle:
        return float(WAIT_SLICE_SECONDS)
    return min(max((opening_at - now).total_seconds(), 0.0), float(WAIT_SLICE_SECONDS))


def _announce(result: SendJobResult, opening_at: datetime, announced: str,
              done: PassResult) -> str:
    """Say what we are waiting for — once per answer, not once per slice.

    A run that waits out a night wakes 130 times and hears the same thing every time.
    Printing that is not narration, it is noise that buries the one line where the
    answer changed, so the line is compared to the last one printed and only a new
    answer is announced. Returns what is now on screen.
    """
    line = (f"{result.progress} · {done.holding} · "
            f"next conversation can open {_when(opening_at)}")
    logger.log(logging.INFO if line != announced else logging.DEBUG, "%s", line)
    return line


def _when(moment: datetime) -> str:
    """A waiting time as an operator reads it — *in 4m*, or the hour it resumes.

    Minutes for a spacing clock and a wall-clock time for anything longer: "in 12h 41m"
    is arithmetic the reader has to do again to know whether to leave it running.
    """
    from django.utils import timezone

    from cold_outreach.core.sending_window import operator_timezone

    seconds = (moment - timezone.now()).total_seconds()
    if seconds <= 0:
        return "now"
    if seconds < 3600:
        return f"in {round(seconds / 60)}m"
    local = timezone.localtime(moment, operator_timezone())
    return f"at {local:%H:%M} on {local:%A}"


# ── What is left to do ────────────────────────────────────────────


def _next_opening() -> datetime | None:
    """When the pool could next open a conversation — ``None`` when there is no box."""
    from cold_outreach.emails.models import Mailbox

    return Mailbox.objects.next_first_email_at()


def _anyone_waiting() -> bool:
    """Whether a lead is still waiting for a first email.

    The one gate a wait cannot resolve: the pool grows by ingest, and ingest is a
    separate invocation of this command. Sleeping for a lead that cannot arrive is how
    a bounded run turns into a process the operator has to remember to kill.
    """
    from cold_outreach.leads.pools import emailable_deals

    return emailable_deals().exists()


# ── Endings ───────────────────────────────────────────────────────


def _stop(result: SendJobResult, because: str, detail: str) -> SendJobResult:
    """Record why the run ended. Whether *that* is a failure is ``reached``'s question —
    a drained pool ends `send all` successfully and `send 5` short."""
    result.stopped_because = because
    result.detail = f"{result.progress} — {detail}"
    return result


def _interrupted(result: SendJobResult) -> SendJobResult:
    """Ctrl-C hands back the conversations already opened, not a stack trace.

    They are sent — the interrupt cannot unsend them, and a traceback would leave the
    operator guessing how many people just heard from them.
    """
    return _stop(result, INTERRUPTED, "interrupted")
