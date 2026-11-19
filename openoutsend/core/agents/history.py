# openoutsend/core/agents/history.py
"""The history a message is written from: everything that passed with one person.

The model is handed the conversation as a conversation: Pydantic AI's
``message_history``, each email we sent one of its own turns and each email they sent
one of theirs, oldest first. The prompt for the message being written comes after it,
and the instructions it is written under are the system prompt of that one call::

    user        Every email with them so far, oldest first.
    assistant   the E0 email, as sent
    user        [no reply for 3 business days]
    assistant   the E1 email, as sent
    user        their reply
    user        the prompt for this message

**What was said, never what we were told.** Each of our emails was written from a prompt
of its own, and the mail log keeps it (``Message.prompt``), but the model never reads it
again: an old step's instructions (its language, its offer, a price that was next) would
sit beside today's and disagree with them. The model learns where things stand from what
was said.

**Verbatim and uncapped.** Threads here run to a handful of emails, so the record itself
fits.

**Silence is part of it.** Between two of our sends with nothing from them, and after
our last one, the history says how long it has been, so the model knows it is writing
into silence rather than inferring it from timestamps.

**Turns only** (``Thread.turns``): a bounce or an out-of-office is a fact about the
thread, not something anybody said, so it never reaches the model to be answered.

``transcript`` is the same history as text, for a session that decides in the model's
place and reads it in a brief.
"""
from __future__ import annotations

from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart

from openoutsend.core.business_time import business_days_between

OPENING = "Every email with them so far, oldest first."

# Who said a line of the history: they, or we.
THEM, US = "them", "us"


def history(lead, now) -> list[ModelMessage]:
    """Everything that passed with *lead*, oldest first, as the model's message history.

    Empty when nothing has: a first email is written from its prompt alone.
    """
    from openoutsend.core.sending_window import lead_timezone

    return messages(_turns_with(lead), now, lead_timezone(lead))


def messages(turns: list, now, zone=None) -> list[ModelMessage]:
    """*turns*, oldest first, as Pydantic AI messages, with the silences between.

    Lines from the same side in a row are one message. Business days are counted in
    *zone* (UTC when left out).
    """
    grouped: list[tuple[str, list[str]]] = []
    for side, line, _ in _lines(turns, now, zone):
        if grouped and grouped[-1][0] == side:
            grouped[-1][1].append(line)
        else:
            grouped.append((side, [line]))
    return [_message(side, lines) for side, lines in grouped]


def transcript(turns: list, now, zone=None) -> str:
    """*turns* as text, oldest first, for a session reading them in a brief."""
    return "\n\n".join(
        f"{label}:\n{line}" if label else line
        for _, line, label in _lines(turns, now, zone)[1:]
    )


def _lines(turns: list, now, zone) -> list[tuple[str, str, str | None]]:
    """Each line of the history, who said it, and its label in a transcript (none for the
    opening line and a silence), starting with the opening line."""
    if not turns:
        return []
    lines = [(THEM, OPENING, None)]
    for index, turn in enumerate(turns):
        lines.append(_line_of(turn))
        if turn.is_outbound:
            following = turns[index + 1] if index + 1 < len(turns) else None
            if following is None or following.is_outbound:
                gap = _silence(turn.sent_at, following.sent_at if following else now, zone)
                lines.append((THEM, gap, None))
    return lines


def _turns_with(lead) -> list:
    """Every turn in every thread of every deal *lead* is in, in the order it happened."""
    from openoutsend.emails.models import Message, Thread
    from openoutsend.emails.models.maillog import TURN_KINDS

    return list(
        Message.objects
        .filter(thread__in=Thread.objects.filter(deals__lead=lead), kind__in=TURN_KINDS)
        .order_by("sent_at", "pk")
    )


def _line_of(turn) -> tuple[str, str, str]:
    """One turn as the model reads it: our email with its date and subject, or theirs.

    An inbound ``Date`` header can be unparseable, so their side may carry no date;
    ours always does, because the send stamps it.
    """
    when = turn.sent_at.strftime("%Y-%m-%d") if turn.sent_at else "date unknown"
    body = (turn.body_text or "").strip()
    if not turn.is_outbound:
        return THEM, f"[{when}]\n{body}", "THEY WROTE"
    return US, f'[{when}, subject "{turn.subject}"]\n{body}', "YOU SENT"


def _message(side: str, lines: list[str]) -> ModelMessage:
    if side == US:
        return ModelResponse(parts=[TextPart(content="\n\n".join(lines))])
    return ModelRequest(parts=[UserPromptPart(content=line) for line in lines])


def _silence(since, until, zone) -> str:
    """The gap after one of our sends that nobody answered, in the lead's working days.

    Our sends always carry ``sent_at``, and *until* is either the next of ours or now.
    """
    days = business_days_between(since, until, zone)
    return f"[no reply for {days} business day{'' if days == 1 else 's'}]"
