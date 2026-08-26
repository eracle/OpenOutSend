"""The one exception the command line answers with.

An expected failure is an *answer*, not a bug: a campaign name that matches nothing,
a headless run with no configuration to read. It prints as one line on stderr and
exits non-zero, with no traceback — the finder learned this the hard way, and a
sender parsed by the same cron entry owes the same manners.

Anything else raises and keeps its traceback, because a bug that prints like a
configuration error sends the operator after a key that was fine.
"""
from __future__ import annotations


class OutsendError(Exception):
    """An expected failure, phrased for the person who typed the command."""
