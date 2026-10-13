# openoutsend/emails/sender.py
"""Send one outbound email through a Mailbox's SMTP credentials, or Resend's API.

A resend box differs in three places only, all in this module: the opt-out is the
line's ``unsubscribe_url``, the opener names itself in ``References``, and the
message is posted to the API rather than over SMTP (``core/conf.py``, *Transport*).

No error handling by design: a failed send raises and the EMAIL task is marked
FAILED by the daemon, then retried on the next cycle. The mailbox is left
untouched — re-import with fixed credentials to repair a dead box.
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid

from django.utils import timezone

from openoutsend.errors import OutsendError

logger = logging.getLogger(__name__)

SMTP_TIMEOUT_SECONDS = 30


def send_email(
    mailbox,
    to_address: str,
    subject: str,
    body: str,
    *,
    bcc: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    thread=None,
    prompt_line=None,
    prompt: str = "",
    step=None,
    sequence: str = "",
    unsubscribe_url: str = "",
):
    """Send ``body`` from ``mailbox`` to ``to_address``; return its ``Message`` row.

    The mailbox's signature, the opt-out block and the product attribution line
    are appended to ``body`` here rather than at the call sites, so every send —
    opener and follow-up — carries all three, in the order body → signature →
    opt-out → attribution. The matching ``List-Unsubscribe`` header goes on the
    same message: header and body line are two halves of one opt-out, and only
    building them together makes it impossible to ship one without the other.

    **The send joins the mail log before it is attempted**, and what the receiver
    answers is recorded against it: an ``accepted`` event carrying the SMTP queue
    id, or a ``rejected``/``deferred``/``error`` one carrying the refusal. A send
    that is never accepted therefore leaves a row saying so, which is what makes
    "what is my bounce rate?" arithmetic rather than an opinion.

    ``bcc`` (when set) blind-copies the operator's own address so they keep a
    private record of every send; ``send_message`` strips the Bcc header before
    transmission, so the To recipient never sees it. Call sites get it from
    ``operator_bcc`` rather than deciding for themselves.

    ``in_reply_to``/``references`` thread a reply onto an existing email thread
    (both are prior Message-IDs); ``thread`` puts the row in that conversation
    directly, rather than making the log re-derive what the caller already knows.

    ``prompt_line`` is the move the opener was written from
    (``core/prompt_lines.py``), recorded on the row so that which line produced which
    reply is a query rather than a guess. Only an opener has one — a reply answers what
    they wrote — so this is the transport's optional argument and
    ``steps/send.send_first_email``'s required one, which is where losing it would
    actually put a hole in the log.

    ``prompt`` is what the message was written from, as rendered, and ``step`` the
    sequence step (``core/sequences.Step``) when it was one. Both are recorded so the
    next message's history replays what the model was asked, not what the file says
    today (``core/agents/history.py``). ``sequence`` is the sending deal's sequence
    key, recorded on every send a deal makes.

    ``unsubscribe_url`` is the line's own opt-out link, and a resend box cannot send
    without one: with no mailbox to read, a *Reply "stop"* would reach nobody, so the
    link is the opt-out in both the header and the footer (``_opt_out_for``).
    """
    email_message = _build_message(
        mailbox, to_address, subject, body, bcc, in_reply_to, references, unsubscribe_url)
    row = _record_send(mailbox, email_message, body, thread, prompt_line, prompt, step, sequence)
    _deliver(mailbox, email_message, row)
    logger.info("email sent from %s to %s: %s [%s]",
                mailbox.from_address, to_address, subject, email_message["Message-ID"])
    logger.info("%s", _sent_block(subject, body))
    return row


def suppressed(lead) -> bool:
    """True when *lead* may not be emailed — re-read from the DB at send time.

    Ingest checks the same list at the door, but an unsubscribe can land in the
    seconds an LLM call takes, and a row that arrived without an address was never
    checkable on the way in at all. So this is the gate that has to be asked last,
    and it asks the table rather than the in-memory copy the caller selected earlier.
    """
    from openoutsend.leads.suppression import is_suppressed

    return is_suppressed(lead.email)


def unsubscribe_address(from_address: str) -> str:
    """The plus-addressed alias a client-generated unsubscribe is sent to.

    Plus-addressing means no second mailbox, no DNS and no web surface: the alias
    delivers to the same INBOX the daemon already reads over IMAP, so the opt-out
    path is the box's own mail. The mail pass matches on this exact string.

    **This assumes the provider routes ``+`` tags to the base mailbox.** Gmail and
    Google Workspace do, and they are both the documented default (``smtp.gmail.com``
    / ``imap.gmail.com``) and what IceMail provisions, so it holds for the boxes
    this actually runs on. A provider that *rejects* plus-addressing would bounce
    the opt-out, and the recipient — believing they unsubscribed — would get the
    next follow-up and reasonably hit "spam". Nothing verifies the alias round-trips
    at onboarding; connecting a non-Gmail box is the case to check by hand.
    """
    local, _, domain = from_address.partition("@")
    return f"{local}+unsub@{domain}"


def operator_bcc(user) -> str | None:
    """The operator's own address, to blind-copy on every send, or None if they set none.

    Self-hosted means every campaign is theirs: it is their outreach, from their
    mailbox, and they want the thread in their own inbox. (There used to be a second
    answer here — the finder's freemium campaigns sent OpenOutreach's own outreach
    from the operator's box, and got no copy. That campaign is gone and so is the
    branch.)
    """
    return user.email or None


def _sent_block(subject: str, body: str) -> str:
    """What the agent wrote, indented under its subject.

    The ``body`` argument, deliberately — not the assembled message. The signature,
    opt-out line and attribution are fixed text appended to every send, so logging
    them repeats what the operator already knows on every line and buries the one
    part that differs: what the agent actually composed for this lead.

    The operator already receives every one of these in full by BCC, so the log
    discloses nothing they do not already hold.
    """
    indented = "\n".join(f"    {line}" for line in body.rstrip().splitlines())
    return f"    Subject: {subject}\n\n{indented}"


# ── Message assembly ──────────────────────────────────────────────


def _build_message(mailbox, to_address, subject, body, bcc, in_reply_to, references,
                   unsubscribe_url="") -> EmailMessage:
    """Assemble the email with threading headers and a domain-anchored Message-ID.

    **A resend opener names itself in ``References``.** Resend replaces the Message-ID
    we mint with its own, so a later step's ``In-Reply-To`` points at an id the
    recipient never saw. ``References`` passes through untouched, and Gmail threads two
    messages that name the same id there — so the opener carries its own minted id,
    and every later step carries the chain, which starts with it. Tested to Gmail: with
    the id only on the later step, the two arrive as separate conversations.
    """
    if mailbox.is_resend and not unsubscribe_url:
        raise OutsendError(f"{to_address} has no unsubscribe_url, and a resend box "
                           "cannot send without one")
    message = EmailMessage()
    message["Message-ID"] = _mint_message_id(mailbox.from_address)
    message["From"] = mailbox.from_address
    message["To"] = to_address
    if bcc:
        message["Bcc"] = bcc
    message["Subject"] = subject
    if mailbox.is_resend:
        # RFC 8058 one-click: the link takes a POST, so a mail client's own
        # unsubscribe button works without the reader opening a page.
        message["List-Unsubscribe"] = f"<{unsubscribe_url}>"
        message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    else:
        message["List-Unsubscribe"] = _list_unsubscribe(mailbox.from_address)
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = references or in_reply_to
    elif mailbox.is_resend:
        message["References"] = message["Message-ID"]
    signed = _sign(body, mailbox.signature)
    message.set_content(_attribute(_opt_out_for(mailbox, signed, unsubscribe_url)))
    return message


def _list_unsubscribe(from_address: str) -> str:
    """The RFC 2369 ``List-Unsubscribe`` value — a ``mailto:``, never an ``https:``.

    RFC 8058 one-click needs an HTTPS endpoint accepting POST; the self-hosted
    daemon has no web surface, and routing unsubscribes through the hub would make
    every install call home. A ``mailto:`` URI needs no infrastructure and the
    daemon already speaks IMAP, so the opt-out lands where it can be read.
    ``List-Unsubscribe-Post`` is deliberately absent — it is only valid alongside
    an ``https:`` URI, and one-click binds bulk senders at 5,000+ messages/day.
    """
    return f"<mailto:{unsubscribe_address(from_address)}?subject=unsubscribe>"


def _sign(body: str, signature: str | None) -> str:
    """Append the mailbox's sign-off, separated by a blank line.

    Declined ("") or never asked (None) ⇒ body unchanged.
    """
    signature = (signature or "").strip()
    if not signature:
        return body
    return f"{body.rstrip()}\n\n{signature}\n"


OPT_OUT_LINE = "Not interested? Reply \"stop\" and I won't write again."


def _opt_out(body: str) -> str:
    """Append the visible opt-out line, after the signature and before the attribution.

    Plain text rather than a link: a typed reply works in every client, including
    the ones where Gmail declines to render its own unsubscribe button — which, for a
    low-volume box sending one-to-one shaped mail, is most of them. Without this line
    those recipients have no visible exit but the spam button, and a complaint costs
    the sending reputation everything else here is built to protect.

    **The quoted word is load-bearing.** `emails/steps/reply.py` matches it before any
    model is consulted, so the one wording this asks for is honoured by code rather
    than by inference. Reword the sentence freely; keep a word the gate can match.
    """
    return f"{body.rstrip()}\n\n{OPT_OUT_LINE}\n"


UNSUBSCRIBE_LINE = "Not interested? Unsubscribe here and I won't write again: {url}"


def _opt_out_for(mailbox, body: str, unsubscribe_url: str) -> str:
    """The footer's opt-out: the reply word for a mailbox, the link for a resend box.

    A resend box reads no mail, so a typed "stop" would land in a person's forwarded
    inbox and nowhere the code could honour it. The link is the one exit that works
    by itself.
    """
    if not mailbox.is_resend:
        return _opt_out(body)
    return f"{body.rstrip()}\n\n{UNSUBSCRIBE_LINE.format(url=unsubscribe_url)}\n"


ATTRIBUTION = "Sent with OpenOutreach"


def _attribute(body: str) -> str:
    """Append the product attribution line, separated by a blank line.

    Always on, last in the message (after the signature): every recipient of an
    outbound email is a plausible future operator. It names the product without
    linking it — a bare name reads as a footer, a URL reads as an ad, and anyone
    curious enough to act on it can search. The separator matches every other
    block's, so the tail of the message reads as evenly spaced.
    """
    return f"{body.rstrip()}\n\n{ATTRIBUTION}\n"


def _mint_message_id(from_address: str) -> str:
    """A unique RFC-5322 Message-ID anchored to the sending domain.

    Anchoring to the From domain (rather than ``make_msgid``'s default local
    hostname) keeps the Message-ID aligned with the sender and avoids leaking
    the container hostname.
    """
    domain = from_address.rsplit("@", 1)[-1]
    return make_msgid(domain=domain)


# ── Transport ─────────────────────────────────────────────────────


def _record_send(mailbox, email_message: EmailMessage, body: str, thread, prompt_line,
                 prompt: str, step, sequence: str):
    """Write the outbound row *before* the transport runs, and thread it.

    Outbound is authoritative in a way inbound never is: we composed the body, so
    ``body_text`` holds what the agent wrote (before the signature, opt-out and
    attribution, which are identical on every send) and only the headers are kept
    as bytes. It is stored classified and processed — there is nothing to work out
    later about a message we wrote ourselves.
    """
    from openoutsend.core.operator import get_active_user
    from openoutsend.emails import parsing, threads
    from openoutsend.emails.classify import CLASSIFIER_VERSION
    from openoutsend.emails.models import Direction, Kind, Message

    now = timezone.now()
    row = Message.objects.create(
        mailbox=mailbox,
        thread=thread,
        direction=Direction.OUTBOUND,
        message_id=parsing.normalize_id(email_message["Message-ID"]),
        from_address=(email_message["From"] or "").lower()[:320],
        to_address=(email_message["To"] or "").lower()[:320],
        subject=email_message["Subject"] or "",
        in_reply_to=parsing.normalize_id(email_message["In-Reply-To"] or ""),
        references_ids=parsing.referenced_ids(email_message),
        raw=_headers_of(email_message),
        body_text=body,
        prompt_line_id=prompt_line.id if prompt_line else "",
        prompt_line_digest=prompt_line.digest if prompt_line else "",
        prompt=prompt,
        prompt_path=str(step.path) if step else "",
        step=step.number if step else None,
        sequence=sequence,
        sent_at=now,
        kind=Kind.OUTBOUND,
        classified_at=now,
        classifier_version=CLASSIFIER_VERSION,
        processed_at=now,
        owner=get_active_user(),
    )
    threads.assign(row)
    return row


def _headers_of(email_message: EmailMessage) -> bytes:
    """The message's headers as bytes — everything above the body it already stores."""
    header = EmailMessage()
    for name, value in email_message.items():
        header[name] = value
    return header.as_bytes()


def _deliver(mailbox, email_message: EmailMessage, row) -> None:
    """Log into the mailbox over SMTP+STARTTLS and send one message.

    Both answers are recorded against *row*: the ``250`` on the way out — with the
    receiver's own queue id, which is the handle a provider asks for when a
    delivery is disputed and which used to be thrown away — and any refusal on the
    way past, before it is re-raised unchanged. The step still fails and is still
    retried; what changes is that the receiver's word survives the traceback.
    """
    from openoutsend.emails.delivery_policy import record_acceptance, record_failure

    if mailbox.is_resend:
        _deliver_through_resend(mailbox, email_message, row)
        return
    try:
        with _SMTP(mailbox.host, mailbox.port, timeout=SMTP_TIMEOUT_SECONDS) as smtp:
            smtp.starttls()
            smtp.login(mailbox.username, mailbox.password)
            smtp.send_message(email_message)
            record_acceptance(row, *smtp.accepted_response)
    except (smtplib.SMTPException, OSError) as exc:
        record_failure(row, exc)
        raise


# The headers a resend send carries over from the assembled message. Message-ID is not
# among them — Resend writes its own — and From/To/Subject/Bcc are fields of the call.
_RESEND_HEADERS = ("In-Reply-To", "References", "List-Unsubscribe", "List-Unsubscribe-Post")


def _deliver_through_resend(mailbox, email_message: EmailMessage, row) -> None:
    """Post the assembled message to Resend's API, recording its answer like an SMTP one.

    The message is built exactly as for a mailbox and only translated here, so the
    stored headers are the ones that went out. The minted Message-ID is the
    idempotency key: one row, one email, however many times the call is retried.
    """
    from openoutsend.emails import resend
    from openoutsend.emails.delivery_policy import record_acceptance, record_failure

    payload = {
        "from": email_message["From"],
        "to": [email_message["To"]],
        "subject": email_message["Subject"],
        "text": email_message.get_content(),
        "headers": {name: email_message[name] for name in _RESEND_HEADERS
                    if email_message[name]},
    }
    if email_message["Bcc"]:
        payload["bcc"] = [email_message["Bcc"]]
    try:
        email_id = resend.send(mailbox.password, payload,
                               idempotency_key=email_message["Message-ID"])
    except (resend.ResendError, OSError) as exc:
        record_failure(row, exc)
        raise
    record_acceptance(row, None, f"OK {email_id}", queue_id=email_id)


class _SMTP(smtplib.SMTP):
    """``smtplib.SMTP`` that keeps the receiver's answer to DATA.

    ``sendmail`` returns only the refused recipients and drops the final
    ``250 2.0.0 OK <queue id>`` on the floor. That line is the receiver's own
    handle on the message — the one a provider asks for when a delivery is
    disputed — so the smallest possible override keeps it.
    """

    accepted_response: tuple[int | None, bytes] = (None, b"")

    def data(self, msg):
        code, response = super().data(msg)
        self.accepted_response = (code, response)
        return code, response
