# cold_outreach/emails/delivery_policy.py
"""Read the receiver's answer to a send, and decide what it means.

Every send ends in a verdict from the receiving server, and until now that
verdict was thrown away: ``sender.py`` let the exception propagate, the task was
marked FAILED, and the SMTP code — the only direct statement anyone makes about
this mailbox's standing — was lost in the traceback.

The distinction this module exists to draw is that **a failed send is not one
kind of event**. Three things arrive down the same exception channel and mean
completely different things:

- the receiver deferring us (4xx) — *slow down*, and it expects us to retry;
  routine, and a sporadic one costs nothing
- the receiver refusing us (5xx) — either we hit the real daily ceiling or the
  box has been actioned; the two need opposite responses
- nothing to do with the receiver at all — a dropped socket, a rejected
  password. These say nothing whatsoever about reputation, and treating them as
  if they did means a flaky network quietly throttles a healthy mailbox.

There is deliberately no retry ladder here and no rate threshold. A deferred
cold opener is not a message we have accepted responsibility for: the deal stays
READY_TO_EMAIL and ``reconcile`` re-mints it, already spaced by the send pacing.
And capacity does not need an explicit cut — a box that sends less leaves less
in its Sent folder, which ``warmth.py`` reads back the next day.
"""
from __future__ import annotations

import logging
import re
import smtplib
from dataclasses import dataclass

from django.db import models

logger = logging.getLogger(__name__)


class Response(models.TextChoices):
    """What the far end said, reduced to the cases we act on differently."""

    DEFERRED = "deferred", "Deferred (4xx)"
    QUOTA_EXCEEDED = "quota_exceeded", "Daily quota exceeded"
    BLOCKED = "blocked", "Blocked for reputation"
    REFUSED = "refused", "Refused (5xx)"
    AUTH_FAILED = "auth_failed", "Authentication failed"
    TRANSPORT = "transport", "Transport failure (no SMTP response)"


@dataclass(frozen=True)
class Policy:
    """What a ``Response`` means for the mailbox.

    ``from_receiver`` is the load-bearing flag: only a verdict the receiving
    server actually issued is evidence about this box. It gates the growth check
    in ``warmth.py``, so a socket timeout can never be mistaken for pushback.

    ``pause_today`` stops the box for the rest of the day. ``needs_operator``
    marks the case no amount of backing off will fix.
    """

    from_receiver: bool
    pause_today: bool = False
    needs_operator: bool = False


POLICIES: dict[str, Policy] = {
    # Routine. The receiver is pacing us and expects a later retry; reconcile
    # already provides one. Counts as pushback so capacity stops growing.
    Response.DEFERRED: Policy(from_receiver=True),
    # The receiver stating its own daily ceiling — the one number our measured
    # capacity cannot discover on its own. Believe it over our measurement.
    Response.QUOTA_EXCEEDED: Policy(from_receiver=True, pause_today=True),
    # A reputation action. Sending slower does not undo it, so stop and escalate.
    Response.BLOCKED: Policy(from_receiver=True, pause_today=True, needs_operator=True),
    # Some other permanent refusal — usually about the recipient, not us.
    Response.REFUSED: Policy(from_receiver=True),
    # Credentials, not standing. The box is unusable until repaired, but its
    # reputation is untouched — do not let this depress measured capacity.
    Response.AUTH_FAILED: Policy(from_receiver=False, pause_today=True, needs_operator=True),
    # We never reached the receiver. No information about anything.
    Response.TRANSPORT: Policy(from_receiver=False),
}


def policy_for(response: str) -> Policy:
    """The policy for a ``Response``; transport (no information) if unrecognised."""
    return POLICIES.get(response, POLICIES[Response.TRANSPORT])


# Derived from the table above rather than listed again, so a policy can only be
# changed in one place.
PAUSING_RESPONSES = frozenset(r for r, p in POLICIES.items() if p.pause_today)
RECEIVER_RESPONSES = frozenset(r for r, p in POLICIES.items() if p.from_receiver)


# ── Classification ────────────────────────────────────────────────────

# Gmail reports the enhanced status inline ("550-5.4.5 Daily user sending quota
# exceeded"), and it is more specific than the bare code — 5.4.5 and 5.7.1 are
# both 550 but mean entirely different things.
_ENHANCED_STATUS = re.compile(rb"\b([2-5])\.(\d{1,3})\.(\d{1,3})\b")


@dataclass(frozen=True)
class Verdict:
    """One failed send, read: what the far end said and how we understand it."""

    response: str
    smtp_code: int | None
    enhanced_status: str
    detail: str


def classify(exc: Exception) -> Verdict:
    """Classify a failed send.

    ``smtp_code`` is None when the failure never produced an SMTP response,
    which is itself the signal that the failure carries no reputation meaning.
    """
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return _verdict(Response.AUTH_FAILED, exc.smtp_code, exc.smtp_error)

    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        # Per-recipient dict rather than a single response; we send to one
        # recipient at a time, so the first entry is the whole story.
        code, error = next(iter(exc.recipients.values()), (None, b""))
        return _verdict(_from_code(code, error), code, error)

    if isinstance(exc, smtplib.SMTPResponseException):
        return _verdict(_from_code(exc.smtp_code, exc.smtp_error), exc.smtp_code, exc.smtp_error)

    return _verdict(Response.TRANSPORT, None, str(exc))


def _verdict(response: str, code: int | None, error) -> Verdict:
    status = _enhanced_status(error)
    return Verdict(
        response=response,
        smtp_code=code,
        enhanced_status=".".join(str(part) for part in status) if status else "",
        detail=_detail(error),
    )


def _from_code(code: int | None, error) -> str:
    """Map an SMTP code plus its enhanced status onto a ``Response``."""
    if code is None:
        return Response.TRANSPORT
    if 400 <= code < 500:
        return Response.DEFERRED
    if code < 500:
        return Response.TRANSPORT

    status = _enhanced_status(error)
    if status == (5, 4, 5):
        return Response.QUOTA_EXCEEDED
    if status and status[1] == 7:
        # 5.7.x is the policy/reputation class — blocked, not merely refused.
        return Response.BLOCKED
    return Response.REFUSED


def _enhanced_status(error) -> tuple[int, int, int] | None:
    """The ``(class, subject, detail)`` enhanced status in a server reply, if any."""
    raw = error if isinstance(error, bytes) else str(error).encode("utf-8", "replace")
    match = _ENHANCED_STATUS.search(raw)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


# ── Undeliverable addresses ───────────────────────────────────────────

# The enhanced statuses that mean *there is nobody at this address*, as opposed to
# *not right now* or *not from you*. Listed one by one rather than matched as the
# 5.1.x class, because the list is the policy and a reader has to be able to audit
# it without knowing RFC 3463 by heart.
_DEAD_ADDRESS_STATUSES = frozenset({
    "5.1.1",   # no such user
    "5.1.2",   # no such domain
    "5.1.3",   # the address is malformed and never could have worked
    "5.1.6",   # mailbox moved, no forwarding address
    "5.1.10",  # null MX — the domain publishes that it accepts no mail at all
    "5.2.1",   # the mailbox exists and is disabled
})

# Two absences are deliberate and load-bearing.
#
# **5.7.x is not here**, and it is the one that would do the damage. It is the
# policy/reputation class — *we are blocked* — a statement about this sending box,
# not about the recipient. Suppressing on it would delete perfectly good prospects
# from every future campaign for as long as our standing was poor, turning a
# recoverable reputation dip into permanent, invisible list attrition. It already
# has the right home: `Response.BLOCKED` pauses the box and calls for an operator.
#
# **5.2.2 (mailbox full) is not here** either: a full mailbox empties, and the
# person is real. It is a deferral wearing a 5.x code.


def address_is_dead(enhanced_status: str) -> bool:
    """True when *enhanced_status* is the receiver saying this address is gone.

    Conservative on purpose: a blank, malformed or unlisted status is never dead.
    Mail stops going to somebody only on the receiver's own explicit statement that
    there is nobody there — a false positive here deletes a real prospect from every
    campaign, now and later, and nothing in the system would ever say why.
    """
    return enhanced_status in _DEAD_ADDRESS_STATUSES


def stop_mailing(address: str, enhanced_status: str) -> int:
    """Suppress *address* as undeliverable. Returns the deals it ended.

    The single place a delivery failure becomes a suppression, so the NDR path and
    the synchronous-refusal path cannot drift: they carry the same enhanced status
    and mean the same thing to the receiver, and the only difference between them is
    which door the answer came back through.
    """
    from cold_outreach.leads.models import DealState
    from cold_outreach.leads.suppression import suppress_email

    return suppress_email(
        address,
        reason=f"undeliverable ({enhanced_status})",
        end_state=DealState.UNDELIVERABLE,
    )


def _detail(error) -> str:
    """The server's own words, decoded and trimmed for storage."""
    if isinstance(error, bytes):
        error = error.decode("utf-8", errors="replace")
    return " ".join(str(error).split())[:255]


# ── Recording ─────────────────────────────────────────────────────────


# How a ``Response`` shows up in the delivery log. The status is the *fact* — what
# happened to this send — and the response is the reading of it; a rejection and a
# refused password are one status apart and worlds apart in meaning, which is
# exactly why both are stored.
_STATUS_FOR = {
    Response.DEFERRED: "deferred",
    Response.QUOTA_EXCEEDED: "rejected",
    Response.BLOCKED: "rejected",
    Response.REFUSED: "rejected",
    # Nothing reached the receiver, so nothing about delivery is known — that is a
    # different fact from being turned down, and it is recorded as one.
    Response.AUTH_FAILED: "error",
    Response.TRANSPORT: "error",
}


def record_acceptance(message, smtp_code: int | None, response):
    """Record the ``250`` — the receiver taking responsibility for this message.

    A successful send used to be the *absence* of a row, which is why 590 of them
    left nothing to count. Recording it is what makes a rate a rate: bounces over
    accepted sends, rather than bounces over a number nobody kept.
    """
    from cold_outreach.emails.models import DeliveryEvent

    text = _detail(response)
    return DeliveryEvent.objects.create(
        message=message,
        status=DeliveryEvent.Status.ACCEPTED,
        smtp_code=smtp_code,
        queue_id=_queue_id(text),
        detail=text,
    )


def record_failure(message, exc: Exception):
    """Classify a failed send, persist the event, and return its ``Policy``.

    The single entry point: callers hand over the exception and act on the
    policy, rather than each deciding for itself what a code means.
    """
    from cold_outreach.emails.models import DeliveryEvent

    verdict = classify(exc)
    DeliveryEvent.objects.create(
        message=message,
        status=_STATUS_FOR[verdict.response],
        response=verdict.response,
        smtp_code=verdict.smtp_code,
        enhanced_status=verdict.enhanced_status,
        detail=verdict.detail,
    )
    policy = policy_for(verdict.response)
    address = message.mailbox.from_address
    logger.warning("send verdict on %s: %s (code=%s) %s",
                   address, verdict.response, verdict.smtp_code, verdict.detail)
    if policy.needs_operator:
        logger.error("%s needs attention — %s: %s", address, verdict.response, verdict.detail)

    # A refusal at the door and a report an hour later carry the same enhanced status
    # and mean the same thing about the recipient. Reading only one of them would let
    # a dead address survive purely by the route its verdict came home.
    if address_is_dead(verdict.enhanced_status):
        ended = stop_mailing(message.to_address, verdict.enhanced_status)
        logger.warning("%s is undeliverable (%s) — %d open deal(s) ended",
                       message.to_address, verdict.enhanced_status, ended)
    return policy


_QUEUE_ID = re.compile(r"\bOK\b[\s:]+(\S+)", re.IGNORECASE)


def _queue_id(text: str) -> str:
    """The receiver's queue id out of its acceptance line, or ""."""
    match = _QUEUE_ID.search(text)
    return match.group(1)[:128] if match else ""
