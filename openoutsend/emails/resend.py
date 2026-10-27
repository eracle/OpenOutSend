# openoutsend/emails/resend.py
"""The Resend API — the whole of what a ``resend`` install says to its provider.

One call or two per thing the transport replaces (``core/conf.py``, *Transport*):

    verify          GET  /domains                 the key works, and the From domain is verified
    send            POST /emails                  one message; Resend's id is its handle
    recent_emails   GET  /emails                  what became of the messages sent lately
    received_emails GET  /emails/receiving        what Resend Inbound took in lately
    received_raw    GET  /emails/receiving/{id}   one of those, as the bytes that arrived

**A full-access key, not a sending one.** A sending-only key can post an email and
read nothing back, so the bounces this install learns about would be none, and the
replies too. ``verify`` asks for the domain list, which only a full-access key may
read, so the wrong key is caught when the box is connected rather than as a silent
absence of bounces.

Standard library only: a handful of JSON calls do not earn an HTTP client dependency.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime
from urllib.parse import urlencode

from django.utils.dateparse import parse_datetime

API_BASE = "https://api.resend.com"
TIMEOUT_SECONDS = 30
# Resend sits behind Cloudflare, which refuses urllib's default ``Python-urllib/3.x``.
USER_AGENT = "openoutsend"

# The list endpoint's largest page, and how far back one poll pages. Ten pages is a
# thousand emails — weeks of a 50-a-day install even on an account it shares.
PAGE_SIZE = 100
MAX_PAGES = 10
# Resend allows two requests a second per team; paging stays under it.
PAGE_PAUSE_SECONDS = 0.6


class ResendError(Exception):
    """A refusal from the API: its HTTP status, its error name, and its words."""

    def __init__(self, status: int, name: str, message: str):
        super().__init__(f"{status} {name}: {message}")
        self.status = status
        self.name = name
        self.message = message


def verify(api_key: str, from_address: str) -> tuple[bool, str]:
    """Check the key reads the account and the From domain is verified. ``(ok, reason)``."""
    domain = from_address.rpartition("@")[2].lower()
    try:
        listed = _request("GET", "/domains", api_key).get("data") or []
    except ResendError as exc:
        if exc.status in (401, 403):
            return False, (f"Resend refused the key ({exc.name}) — a resend install needs a "
                           "full-access key, because a sending-only one cannot read bounces back")
        return False, f"Resend answered {exc}"
    except OSError as exc:
        return False, f"could not reach Resend ({exc})"

    statuses = {(entry.get("name") or "").lower(): entry.get("status") for entry in listed}
    if domain not in statuses:
        return False, f"{domain} is not a domain on this Resend account"
    if statuses[domain] != "verified":
        return False, f"{domain} is {statuses[domain]} on Resend, not verified"
    return True, ""


def send(api_key: str, payload: dict, *, idempotency_key: str) -> str:
    """Post one email. Returns Resend's id for it.

    The idempotency key makes a retry of the same message — a timeout after Resend
    took it — come back with the first id rather than a second email.
    """
    answer = _request("POST", "/emails", api_key, body=payload,
                      headers={"Idempotency-Key": idempotency_key})
    return answer["id"]


def recent_emails(api_key: str, *, since: datetime) -> list[dict]:
    """Every email on the account created at or after *since*, newest first.

    Each item carries ``id``, ``to`` and ``last_event`` — the latest thing that
    happened to it, which is all the list says.
    """
    return _listed("/emails", api_key, since=since)


def received_emails(api_key: str, *, since: datetime | None) -> list[dict]:
    """Every email Resend received for the account at or after *since*, newest first.

    ``None`` reads back as far as ``MAX_PAGES`` goes. Each item carries ``id``,
    ``to``, ``cc``, ``created_at`` and ``message_id`` — enough to know whether it is
    already held, not its body.
    """
    return _listed("/emails/receiving", api_key, since=since)


def received_raw(api_key: str, email_id: str) -> bytes | None:
    """The bytes of one received email as they arrived, or None if Resend kept none.

    Two requests: the email's record names a signed download URL for its original
    file, and the file is fetched from it without the key — it is CloudFront's, not
    the API's.
    """
    answer = _request("GET", f"/emails/receiving/{email_id}", api_key)
    url = (answer.get("raw") or {}).get("download_url")
    if not url:
        return None
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return response.read()


def _listed(path: str, api_key: str, *, since: datetime | None) -> list[dict]:
    """One list endpoint's items created at or after *since*, newest first.

    Pages back until a page reaches past *since*, runs out, or ``MAX_PAGES`` is hit.
    """
    emails: list[dict] = []
    after = None
    for page_number in range(MAX_PAGES):
        if page_number:
            time.sleep(PAGE_PAUSE_SECONDS)
        query = {"limit": PAGE_SIZE, **({"after": after} if after else {})}
        page = _request("GET", f"{path}?{urlencode(query)}", api_key)
        items = page.get("data") or []
        for item in items:
            created = parse_datetime(item.get("created_at") or "")
            if since is not None and created is not None and created < since:
                return emails
            emails.append(item)
        if not page.get("has_more") or not items:
            return emails
        after = items[-1]["id"]
    return emails


# ── Transport ─────────────────────────────────────────────────────


def _request(method: str, path: str, api_key: str, *, body: dict | None = None,
             headers: dict | None = None) -> dict:
    """One API call. Returns the JSON answer; raises ``ResendError`` on a refusal.

    A network failure raises as the ``OSError`` urllib gives (``URLError`` is one),
    which the callers already read as *no answer*, not as a verdict.
    """
    request = urllib.request.Request(
        f"{API_BASE}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raise _refusal(exc) from exc


def _refusal(exc: urllib.error.HTTPError) -> ResendError:
    """The API's error body as a ``ResendError``, or the bare status when it sent none."""
    try:
        answer = json.loads(exc.read() or b"{}")
    except ValueError:
        answer = {}
    return ResendError(exc.code, answer.get("name") or "", answer.get("message") or str(exc.reason))
