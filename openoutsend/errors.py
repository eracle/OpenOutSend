"""The one exception the command line answers with.

An expected failure is an *answer*, not a bug: a required credential missing,
a headless run with no configuration to read. It prints as one line on stderr and
exits non-zero, with no traceback — the finder learned this the hard way, and a
sender parsed by the same cron entry owes the same manners.

Anything else raises and keeps its traceback, because a bug that prints like a
configuration error sends the operator after a key that was fine.
"""
from __future__ import annotations


class OutsendError(Exception):
    """An expected failure, phrased for the person who typed the command."""


class DraftPending(OutsendError):
    """`send --agent-draft` stopped one deal short of the opener call it opted out of.

    Carries the deal's own fields as ``payload`` — the same shape the outreach agent's
    prompt is built from, so an agent answering isn't missing anything the real
    `AI_MODEL` path would have seen. ``main()`` renders ``payload`` on its own line
    under ``--json``, alongside the plain ``error: draft_pending: <message>`` either
    way — see the module docstring in ``core/agent_draft.py``.
    """

    def __init__(self, message: str, payload: dict) -> None:
        self.payload = payload
        super().__init__(message)
