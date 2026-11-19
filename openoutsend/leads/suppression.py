"""The suppression list — the door, and the last gate before a message is built.

Three writers reach it: the mail pass, when an opt-out lands in the box
(`emails/project.py`), a conversation's turn that read a worded one
(`leads/conversing.py`), and ingest, which checks it on every row that arrives. One reader matters more than either — `emails/sender.suppressed`,
which asks again at send time, because an unsubscribe can land in the seconds an LLM
call takes.

Addresses are normalised on the way in and on the way out, so a capitalised reply
address and a lowercased export row are the same person here.
"""
from __future__ import annotations

import logging

from openoutsend.leads.models import Deal, Outcome, Suppression

logger = logging.getLogger(__name__)


def normalize(address: str | None) -> str:
    """The comparable form of an email address: stripped and lowercased."""
    return (address or "").strip().lower()


def is_suppressed(address: str | None) -> bool:
    """True when *address* is on the list. Blank is never suppressed — it is unknown.

    A blank address has nothing to check, which is why the door cannot be the only
    gate: a row ingested without one is stored, and the check that matters for it
    happens at send, once an enrichment has filled the address in.
    """
    email = normalize(address)
    return bool(email) and Suppression.objects.filter(email=email).exists()


def suppress_email(
    address: str | None,
    *,
    reason: str = "",
    outcome: str = Outcome.UNSUBSCRIBED,
    leaving=None,
) -> int:
    """Put *address* on the list for good and end its open conversations.

    ``leaving`` is a deal whose own step is suppressing the address and ends it by its
    own move (a turn that read a worded unsubscribe), so it is not ended a second time.

    Returns the number of deals ended — the deals rather than the leads, because the
    duty is to stop emailing and a deal is what would have emailed them. A deal that
    already reached `COMPLETED` is left exactly as it was: an opt-out arriving after a
    conversation ended changes nothing about how it ended.

    ``outcome`` says *why the mailing stopped*. Somebody who asked to stop ends
    `unsubscribed`; an address the receiver says is dead ends `undeliverable`
    (`emails/delivery_policy.stop_mailing`). Both are `COMPLETED` and neither is
    sendable — the difference is what the funnel says happened, and it is a column of
    choices rather than a second terminal state, so every ending is read the same way.

    Idempotent, and the first suppression wins. Re-suppressing keeps the original
    timestamp and reason, since when somebody *first* asked is the fact worth having.
    """
    email = normalize(address)
    if not email:
        return 0

    _, created = Suppression.objects.get_or_create(email=email, defaults={"reason": reason})
    # ``iexact``, because the address on a lead is whatever its producer wrote. Ingest
    # normalises on the way in, but the duty is to the person, not to a casing, and a
    # row that arrived by any other route must not slip through.
    #
    # Nothing here can overwrite a verdict somebody else reached: a deal that has one
    # is `COMPLETED`, and only the open ones are taken. Each is ended by its own move,
    # so the deal machine's log says which suppression ended it.
    ended = _end_open_deals(email, outcome, leaving)
    logger.info(
        "suppressed %s (%s) — %d open deal(s) ended",
        email, reason or "no reason given", ended,
    )
    if not created:
        logger.debug("%s was already suppressed", email)
    return ended


def _end_open_deals(email: str, outcome: str, leaving) -> int:
    """Fire each open deal of *email*, whose state's step ends it with *outcome*. Returns how many ended.

    A deal whose sequence folder this run cannot find is left open and said so: the
    address is on the list, so nothing can be sent to it, and the deal is ended by the
    next pass that has its folder.
    """
    from openoutsend.core.sequences import SequenceError
    from openoutsend.leads.lifecycle import machine_of

    ended = 0
    open_deals = Deal.objects.filter(lead__email__iexact=email).open().select_related("lead")
    if leaving is not None:
        open_deals = open_deals.exclude(pk=leaving.pk)
    for deal in open_deals:
        deal.outcome = outcome
        try:
            move = machine_of(deal).fire(deal.pk, entity=deal)
        except SequenceError as error:
            logger.error("%s is suppressed, and its deal in %r stays open until a run "
                         "has that folder: %s", email, deal.sequence, error)
            continue
        ended += move is not None
    return ended
