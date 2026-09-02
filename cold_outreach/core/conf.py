# cold_outreach/core/conf.py
"""Constants for the send guards, carried over from OpenOutreach with their reasoning.

The three guards — warm capacity, send pacing and the sending window — came here with
the code they govern. They are complementary rather than redundant: receivers punish
*rate* and *volume* separately, and a recipient reads the *hour*, so no one of them
covers the other two.
"""
from __future__ import annotations

from pathlib import Path

# ----------------------------------------------------------------------
# Paths
# ----------------------------------------------------------------------
PROMPTS_DIR = Path(__file__).parent / "templates" / "prompts"

# ----------------------------------------------------------------------
# Warm capacity (emails/warmth.py) — the per-box daily send ceiling, *measured*
# rather than declared. A fixed number can only ever be wrong in one of two
# directions: it throttles a box that has been carrying more for months, or it
# hands a box connected an hour ago the volume of a seasoned one. So the box
# tells us instead — its own Sent folder is the record of what it has actually
# sustained, which is the only warmth signal available at cold-outreach volume
# (Google Postmaster needs ~100+/day to Gmail before it reports anything).
#
# Read the trailing window's daily counts, take the 75th percentile of the days
# it actually sent (mean is dragged down by idle days, max is set by a single
# anomaly), and allow a step above it. The step is multiplicative because the
# history is *self-referential*: a box's Sent folder is mostly this sender's own
# output, so an additive step makes the measurement a one-way ratchet — throttled
# to 5/day, a +2 step needs three weeks to climb back. It only applies when the
# window is clean; a failed send holds capacity at what was already demonstrated.
#
# **The step is bounded twice, and the second bound is the whole warmup.** Measured
# volume says what the box has carried; *yesterday's allowance* says what it has
# carried as cold volume, and only the second one is warmup. Without that second
# bound, a personal mailbox with months of human mail in its Sent folder is handed
# 25/day on its first measurement — a box switched on at full throttle, which is
# the pattern that lands at 61% inbox placement against 94% for one that ramped.
# So capacity may exceed yesterday's allowance by the growth factor and no more,
# and every box starts on the bottom rung whatever is in its Sent folder.
# **Downward moves are not bounded**: a receiver's verdict lands the day it arrives.
#
# ×1.25 a day off the floor walks 5→6→8→10→12→15→18→22→27→33→41→51→63→78 — the
# ~three weeks to 65–80/day that the field converges on, and what Gmail's own
# "start with a low sending volume and slowly increase the volume over time" asks
# for. ×1.5 reached the rail in ten days, which is a ramp only in name.
#
# The floor lets a box with no history start somewhere. The ceiling is not a
# number of its own at all — it is *derived* from the send pacing and the sending
# window below (``WARM_CEILING_SENDS``, defined there because it cannot be stated
# before the interval it divides), then held down to what a mailbox survives. It
# used to be a declared 50, the top of the 30–50/day band cold-email practice
# converges on for a warmed Google Workspace box; that figure was folklore nobody
# measured, and pinning volume an order below the rate made the two guards
# redundant rather than complementary. The derivation is not folklore, but it is
# not evidence either: the pacing permits 180/day, while measured spam rate per
# box runs ~0.3% under 50/day, ~1.4% at 75–100, ~3.9% at 100–150 and breaks past
# that. So the rail is the lower of what the clock allows and what a box carries
# without paying for it — a derivation that outruns the evidence is still a guess.
# ----------------------------------------------------------------------
WARM_HISTORY_DAYS = 30      # trailing window of Sent history to measure
WARM_GROWTH_FACTOR = 1.25   # step above demonstrated volume, when the window is clean
WARM_FLOOR_SENDS = 5        # a box with no history still sends this much
WARM_SAFE_SENDS = 100       # the most one box carries before spam rate turns on it
# The bounce rate above which a box is damaging itself and must send *less* — the
# one rule in the measurement that points downwards. 5% is the line the major
# receivers and every ESP draw between "some addresses go stale" and "this sender
# does not know who it is mailing"; past it, reputation degrades on its own. It is
# checkable at cold-outreach volume, unlike anything Postmaster Tools reports, and
# it is only computable at all now that every accepted send leaves a row.
WARM_BOUNCE_TOLERANCE = 0.05
# WARM_CEILING_SENDS — derived from the pacing constants; see below.

# ----------------------------------------------------------------------
# Send pacing (emails/steps/send.py) — the minimum gap between two *first*
# emails from one mailbox. Replies are exempt: a reply is not cold volume, and
# answering someone who wrote to you within minutes is more human, not less.
# Receivers rate-limit on *burst*, not on the daily total: Gmail answers an
# unusual sending rate with a 421 4.7.0 deferral regardless of how far below the
# daily quota you are. Measured against a real box, an unpaced sender drains a
# day's openers back-to-back at its own loop time (~11s apart, 40 messages inside
# one hour) — a machine signature, and several times the ~20/hour Gmail is
# observed to tolerate.
#
# The floor is the low end of the 3–8 minute band the cold-email field converges
# on, and the jitter sits *on* that floor rather than spanning the band: the
# realized gap is 3.5–4.5 minutes. Concentrating the day's volume into a 12-hour
# window (below) costs half the clock, and the gap is the only place to pay for
# it — a receiver reads the rate, and ~15/hour is still under the ~20/hour Gmail
# is observed to tolerate. Jitter is not decoration: a send exactly every 180s is
# as machine-shaped as no spacing at all, which is why the floor keeps a spread
# around it rather than being tightened to a fixed 4 minutes.
#
# Per box rather than pool-wide (``Mailbox.next_send_at``): the daily ceiling is
# already per box, two mailboxes are two sending identities, and one receiver's
# rhythm says nothing about the other's.
#
# This bounds the *rate*, and the rate sets the day: a sender that never sends
# twice inside one interval cannot exceed its sending window divided by the mean
# interval, whatever any ceiling says. So the warm rail is *computed* from the
# pacing and the window rather than declared beside them — one number, one place
# to change it. Widen either and the daily rail widens with it; tighten either
# and the rail follows, with no second constant left behind stating the old
# arithmetic.
#
# The daily measurement still decides where a box sits on its way up — a young
# box is held at its demonstrated volume however much time the clock leaves — but
# it no longer holds a warmed box an order of magnitude below what the pacing
# permits. Burst throttling is the failure mode the interval guards; the ramp
# guards the other one.
# ----------------------------------------------------------------------
MIN_SEND_INTERVAL_SECONDS = 180          # 3 minutes, the hard floor between sends
SEND_INTERVAL_JITTER_MIN_SECONDS = 30    # + U[30, 90] → a 3.5–4.5 minute spread
SEND_INTERVAL_JITTER_MAX_SECONDS = 90

# ----------------------------------------------------------------------
# Follow-ups (leads/pools.awaiting_follow_up, emails/steps/follow_up.py)
#
# **The gaps are constants, not a decision.** An earlier version let the agent pick
# its own interval and re-arm a per-deal countdown; that produced the defect in
# `p2-e2-followup-identity-backoff-sentiment`, where a `wait` verdict failed to push
# the next action out and the same unchanged context was re-read five times in one
# window. Nothing here is scheduled on a row — the gap is measured backwards from
# the mail log, so a pass can be interrupted anywhere and simply asked again.
#
# **Business days, not calendar days.** A Friday opener followed up on Monday has
# waited one working day, and chasing across a weekend reads as a machine counting
# hours (`core/business_time.py`).
#
# Two follow-ups, then the deal ends `unresponsive`. The cap is what stops an open
# thread being pursued forever, and the number is deliberately small: cold volume is
# what damages a sending domain, and a third touch is already likelier to draw a
# complaint than a reply.
# ----------------------------------------------------------------------
FOLLOW_UP_GAPS_BUSINESS_DAYS = (3, 5)    # touch 2 after 3 days, touch 3 after 5 more
MAX_COLD_TOUCHES = len(FOLLOW_UP_GAPS_BUSINESS_DAYS) + 1

# Mean realized gap: the floor plus the expected value of U[min, max].
MEAN_SEND_INTERVAL_SECONDS = MIN_SEND_INTERVAL_SECONDS + (
    SEND_INTERVAL_JITTER_MIN_SECONDS + SEND_INTERVAL_JITTER_MAX_SECONDS) / 2

# ----------------------------------------------------------------------
# Sending window (core/sending_window.py) — the hours a *first* email may leave,
# in the operator's own local time (resolved from their country code).
# Replies are exempt, for the same reason they are exempt from the cap: answering
# someone who wrote to you is not cold volume, and holding a written answer until
# Monday morning is worse for the conversation than sending it at 21:00.
#
# A cold opener that lands at 03:00 is a machine announcing itself. Everything
# else in the send path is shaped to read as a person typing — the pacing, the
# jitter, the per-box identity — and a 24/7 clock undoes it in the one field the
# recipient sees before they open anything. It is also the sender's own interest:
# a message that arrives during the working day is read when it arrives, rather
# than sitting under a night's accumulation.
#
# Weekends stop. Saturday cold email is weaker signal to receivers and to people,
# and ``business_time.is_business_day`` already draws the Mon–Fri line.
#
# The window is the operator's, not the recipient's: we know where the operator
# is (one configured answer) and never where the lead is (the discovery row
# carries a country at best, and the campaign may target several).
# ----------------------------------------------------------------------
SEND_WINDOW_START_HOUR = 8   # inclusive — no first email before 08:00 local
SEND_WINDOW_END_HOUR = 20    # exclusive — none from 20:00 on

SEND_WINDOW_SECONDS = (SEND_WINDOW_END_HOUR - SEND_WINDOW_START_HOUR) * 3600

# The rail: sends a single box could emit inside one window at that mean gap
# (180 today), held down to what one box carries safely (100). Floored, so the
# rail never claims a send the clock has no room for — the window halves the day,
# and a ceiling still derived from 24 hours would promise volume the pacing cannot
# deliver before 20:00. Taking the lower of the two keeps both bounds live:
# tighten the pacing or narrow the window and the rail follows it down, and
# nothing here ever promises volume a mailbox pays for in spam rate.
WARM_CEILING_SENDS = min(int(SEND_WINDOW_SECONDS / MEAN_SEND_INTERVAL_SECONDS),
                         WARM_SAFE_SENDS)

# ----------------------------------------------------------------------
# The wait (send_job.py) — how long `outsend send N` sleeps between passes when
# the clocks above are all that stand between it and the next conversation.
#
# **Waiting is not idling.** A run holding out for a spacing clock, or for tomorrow's
# window, still owns the mailbox — and a reply arriving during that wait must not sit
# unread until whatever unblocks the goal. So the wait is spent in slices, each ending
# in a whole pass: the mail is read, replies are answered, and only the *openers* are
# on hold. Five minutes is the cadence a mailbox needs, and it is also the longest a
# Ctrl-C can go unnoticed — the second reason not to sleep the wait in one go.
# ----------------------------------------------------------------------
WAIT_SLICE_SECONDS = 300
