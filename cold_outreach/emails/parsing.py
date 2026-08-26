# cold_outreach/emails/parsing.py
"""Reading an RFC-5322 message. No database, no network, no opinions.

Everything here is a pure function of bytes, which is what makes the classifier
re-runnable: improve a rule, bump ``CLASSIFIER_VERSION``, and every message ever
received is re-read from the bytes we kept rather than from a mailbox that may no
longer hold it.
"""
from __future__ import annotations

import email
import hashlib
import re
from email.message import Message as EmailMessage
from email.utils import parseaddr, parsedate_to_datetime

from django.utils import timezone

# Headers a plus-addressed alias can land in. Clients rewrite recipients freely and
# some providers only record the tagged address in a delivery header, so an opt-out
# is looked for in all of them rather than in ``To`` alone.
RECIPIENT_HEADERS = ("To", "Cc", "Delivered-To", "X-Original-To", "Envelope-To")

# Message-IDs as they appear inside References / In-Reply-To.
_MESSAGE_ID = re.compile(r"<[^<>@\s]+@[^<>@\s]+>")


def parse(raw: bytes) -> EmailMessage:
    """The message these bytes are."""
    return email.message_from_bytes(raw)


# ── Identity and threading ────────────────────────────────────────


def normalize_id(message_id: str) -> str:
    """One Message-ID, without its angle brackets and surrounding whitespace.

    Stored bare, always. Servers differ on whether they hand the brackets back at
    send time, and a store that keeps whichever form arrived makes every lookup
    ask for both — which is exactly the kind of near-match a threading bug hides in.
    """
    return (message_id or "").strip().strip("<>").strip()


def message_id_of(msg: EmailMessage, raw: bytes) -> str:
    """This message's identity: its ``Message-ID``, else a digest of its bytes.

    The fallback is a real identity rather than a guess, because it is derived
    from the thing itself — two fetches of the same message agree, two different
    messages do not collide, and the row stays idempotent under a re-walk.
    """
    header = normalize_id(msg.get("Message-ID", ""))
    return header or f"sha256:{hashlib.sha256(raw).hexdigest()}"


def referenced_ids(msg: EmailMessage) -> list[str]:
    """Every Message-ID this message names, normalized, oldest-first, deduplicated.

    ``References`` carries the chain and ``In-Reply-To`` the immediate parent —
    which is the **latest** message, not the root. A reply that carries only
    ``In-Reply-To`` is exactly the case root-matching drops on the floor.
    """
    raw = " ".join(filter(None, (msg.get("References"), msg.get("In-Reply-To"))))
    seen: dict[str, None] = {}
    for found in _MESSAGE_ID.findall(raw):
        seen.setdefault(normalize_id(found), None)
    return list(seen)


def in_reply_to(msg: EmailMessage) -> str:
    """The immediate parent's Message-ID, or ""."""
    found = _MESSAGE_ID.findall(msg.get("In-Reply-To") or "")
    return normalize_id(found[0]) if found else ""


# ── Addresses, subject, clock ─────────────────────────────────────


def sender_address(msg: EmailMessage) -> str:
    """The ``From`` address, lowercased, without the display name."""
    return parseaddr(msg.get("From", ""))[1].lower()


def recipient_address(msg: EmailMessage) -> str:
    """The first ``To`` address, lowercased."""
    return parseaddr(msg.get("To", ""))[1].lower()


def addressed_to(msg: EmailMessage, alias: str) -> bool:
    """True when *alias* appears in any recipient header."""
    alias = alias.lower()
    return any(alias in (msg.get(header) or "").lower() for header in RECIPIENT_HEADERS)


def subject_of(msg: EmailMessage) -> str:
    """The ``Subject`` header, collapsed to one line and trimmed for storage."""
    return " ".join((msg.get("Subject") or "").split())[:998]


def sent_at(msg: EmailMessage):
    """Timezone-aware send time from the ``Date`` header, or None if unparseable."""
    raw = msg.get("Date")
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else timezone.make_aware(parsed)


# ── Body ──────────────────────────────────────────────────────────


def body_text(msg: EmailMessage) -> str:
    """The text/plain body, stripped of the quoted reply history.

    Prefers the first ``text/plain`` part (skipping attachments); the whole
    payload is the fallback for a non-multipart message. Quoted history is
    trimmed so ``chat_summary`` and the agent see only the lead's new words.
    """
    return _strip_quoted(_first_text_plain(msg))


def _first_text_plain(msg: EmailMessage) -> str:
    """The decoded text/plain payload, or the bare payload for a simple message."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() != "text/plain":
                continue
            if "attachment" in (part.get("Content-Disposition") or "").lower():
                continue
            return _decode(part)
        return ""
    return _decode(msg)


def _decode(part: EmailMessage) -> str:
    """Decode a part's payload to text, tolerating a missing/wrong charset."""
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


# Common reply-quote openers: "On <date>, <name> wrote:" and its localized kin,
# plus Outlook's "-----Original Message-----" divider.
_QUOTE_MARKERS = re.compile(
    r"^\s*(on .+wrote:|-{2,}\s*original message\s*-{2,}|_{5,})\s*$",
    re.IGNORECASE,
)


def _strip_quoted(text: str) -> str:
    """Drop everything from the first quote marker or the trailing ``>`` block.

    Conservative: cuts at the first recognized "On … wrote:" / "Original Message"
    divider, else at the first line of a contiguous run of ``>``-quoted lines that
    continues to the end. Leaves inline text untouched when there is no clear
    boundary.
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if _QUOTE_MARKERS.match(line):
            return "\n".join(lines[:i]).strip()
    for i, line in enumerate(lines):
        if line.lstrip().startswith(">") and all(
            l.lstrip().startswith(">") or not l.strip() for l in lines[i:]
        ):
            return "\n".join(lines[:i]).strip()
    return text.strip()
