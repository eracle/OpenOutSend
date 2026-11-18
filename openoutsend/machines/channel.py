"""The channel a conversation runs over: the threads kept against one entity.

A thread carries the machine and the entity it belongs to (`Thread.machine`,
`Thread.entity`), written when the entity's first email opens it, so a reply finds
whoever is waiting for it. What is incoming is read off the mail log, never stored:
a thread's newest turn of theirs with no answer of ours after it.
"""
from __future__ import annotations

from openoutsend.emails.models import Direction, Message, Thread
from openoutsend.emails.models.maillog import TURN_KINDS


def threads_of(machine: str, entity: str):
    """The threads kept against *entity* of *machine*, oldest first."""
    return Thread.objects.filter(machine=machine, entity=str(entity)).order_by("pk")


def turns_of(machine: str, entity: str) -> list[Message]:
    """Every turn in the entity's threads, in the order it happened."""
    return list(
        Message.objects
        .filter(thread__in=threads_of(machine, entity), kind__in=TURN_KINDS)
        .order_by("sent_at", "pk")
    )


def incoming(machine: str, entity: str) -> Message | None:
    """The message waiting for an answer: a thread's newest turn of theirs that nothing
    of ours has answered since.

    A step of a sequence (a send that records its ``step``) is not an answer: it was
    going on its day whoever wrote, so whoever wrote before it is still owed one.
    Turns only, so a bounce or an out-of-office is never one. With several threads
    waiting, the one stored first.
    """
    waiting = []
    for thread in threads_of(machine, entity):
        unanswered = _unanswered(thread)
        if unanswered is not None:
            waiting.append(unanswered)
    return min(waiting, key=lambda message: message.pk) if waiting else None


def _unanswered(thread) -> Message | None:
    """*thread*'s newest turn of theirs, unless an answer of ours follows it."""
    unanswered = None
    for turn in thread.turns().select_related("thread__mailbox"):
        if not turn.is_outbound:
            unanswered = turn
        elif turn.step is None:
            unanswered = None
    return unanswered


def open_thread(machine: str, entity: str, mailbox, to_address: str, subject: str, body: str,
                *, prompt: str = "", unsubscribe_url: str = "") -> Message:
    """Send the email that opens a thread kept against *entity*; returns its row.

    The thread is made first, so the send is in it from the row it writes. A refused
    send raises (`sender.Refused`) inside the caller's transaction, which takes the
    thread back with it.
    """
    thread = Thread.objects.create(mailbox=mailbox, machine=machine, entity=str(entity))
    return _send(mailbox, to_address, subject, body, thread=thread, prompt=prompt,
                 unsubscribe_url=unsubscribe_url)


def answer(message: Message, body: str, *, prompt: str = "", unsubscribe_url: str = "") -> Message:
    """Send *body* as the answer to *message*, in its thread, from the box it reached."""
    from openoutsend.emails.threads import message_ids

    thread = message.thread
    chain = message_ids(thread)
    return _send(
        thread.mailbox, message.from_address, _reply_subject(thread), body,
        thread=thread, prompt=prompt, unsubscribe_url=unsubscribe_url,
        in_reply_to=chain[-1] if chain else None,
        references=" ".join(chain) or None,
    )


def _send(mailbox, to_address, subject, body, **options) -> Message:
    from openoutsend.core.operator import get_active_user
    from openoutsend.emails.sender import operator_bcc, send_email

    operator = get_active_user()
    return send_email(mailbox, to_address, subject, body,
                      bcc=operator_bcc(operator) if operator else None, **options)


def _reply_subject(thread) -> str:
    """``Re:`` the subject the thread opened with, without stacking a second ``Re:``."""
    opener = thread.turns().filter(direction=Direction.OUTBOUND).first()
    subject = opener.subject if opener else ""
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"
