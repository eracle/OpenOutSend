# openoutsend/core/agents/history.py
"""The history a message is written from: everything that passed with one person.

Every send is one more turn in a running conversation with the model. It reads every
earlier exchange with the lead — across every thread of every deal they are in, all
the way back — and the prompt for the message being written comes after it::

    prompt     E0, as rendered when it was sent
    you sent   the E0 email, as sent
    silence    no reply for 3 business days
    prompt     the follow-up prompt
    you sent   the follow-up
    they wrote their reply

**Verbatim and uncapped.** What this replaces was a rolling fact list beside a window
of the last six turns, which kept a long thread small at the price of a second LLM call
per reply and a summary that could drift from the record. Threads here run to a
handful of emails, so the record itself fits.

**Silence is part of it.** Between two of our sends with nothing from them, and after
our last one, the history says how long it has been, so the model knows it is writing
into silence rather than inferring it from timestamps.

**Turns only** (``Thread.turns``): a bounce or an out-of-office is a fact about the
thread, not something anybody said, so it never reaches the model to be answered.
"""
from __future__ import annotations

from openoutsend.core.business_time import business_days_between


def history(lead, now) -> str:
    """Everything that passed with *lead*, oldest first, ready to go before a prompt.

    ``""`` when nothing has: a first email is written from its prompt alone.
    """
    turns = _turns_with(lead)
    blocks: list[str] = []
    for index, turn in enumerate(turns):
        blocks.extend(_blocks_of(turn))
        if turn.is_outbound:
            following = turns[index + 1] if index + 1 < len(turns) else None
            if following is None or following.is_outbound:
                blocks.append(_silence(turn.sent_at, following.sent_at if following else now))
    return "\n\n".join(blocks)


def _turns_with(lead) -> list:
    """Every turn in every thread of every deal *lead* is in, in the order it happened."""
    from openoutsend.emails.models import Message, Thread
    from openoutsend.emails.models.maillog import TURN_KINDS

    return list(
        Message.objects
        .filter(thread__in=Thread.objects.filter(deals__lead=lead), kind__in=TURN_KINDS)
        .order_by("sent_at", "pk")
    )


def _blocks_of(turn) -> list[str]:
    """One turn as the model reads it: our prompt and email, or their message.

    An inbound ``Date`` header can be unparseable, so their side may carry no date;
    ours always does, because the send stamps it.
    """
    when = turn.sent_at.strftime("%Y-%m-%d") if turn.sent_at else "date unknown"
    body = (turn.body_text or "").strip()
    if not turn.is_outbound:
        return [f"[{when}] THEY WROTE:\n{body}"]
    blocks = [f"[{when}] PROMPT YOU WERE GIVEN:\n{turn.prompt}"] if turn.prompt else []
    blocks.append(f'[{when}] YOU SENT (subject "{turn.subject}"):\n{body}')
    return blocks


def _silence(since, until) -> str:
    """The gap after one of our sends that nobody answered, in working days.

    Our sends always carry ``sent_at``, and *until* is either the next of ours or now.
    """
    days = business_days_between(since, until)
    return f"[NO REPLY for {days} business day{'' if days == 1 else 's'}]"
