"""Read-only IMAP reader for the admin mailbox.

Stdlib only (``imaplib`` + ``email``) — no new dependency, matching the choice
``gateway/mailer.py`` made for SMTP.

READ-ONLY BY CONSTRUCTION
    Every ``select()`` passes ``readonly=True``, which puts the session in IMAP
    EXAMINE mode. In that mode the server itself refuses state changes, so this
    module cannot mark as read, move, or delete a message even if a later edit
    tried to — the requirement "do not modify or delete emails from Gmail" is
    enforced by the protocol, not by our own discipline.

SUBJECT FILTER
    ``subject_matches`` is a strict prefix test on the DECODED subject. It is
    intentionally NOT an IMAP ``SEARCH SUBJECT`` term: that is a substring match
    server-side and would return "RE: JNPA ..." and "ABC JNPA ..." too.
"""
from __future__ import annotations

import email
import hashlib
import imaplib
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .config import MailboxConfig

# Bodies/attachments above this are skipped rather than pulled into memory.
_MAX_ATTACHMENT_BYTES = 32 * 1024 * 1024
_PREVIEW_CHARS = 280


class MailboxError(RuntimeError):
    """A mailbox fault with a message that is SAFE to show a user.

    ``code`` is a stable machine token for the UI; ``str(exc)`` is the friendly
    sentence. The underlying technical detail is logged server-side by the
    caller and never carried in here, so this can be surfaced verbatim.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass
class Attachment:
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    #: Populated only when the caller asked for payloads.
    content: Optional[bytes] = field(default=None, repr=False)


@dataclass
class EmailSummary:
    message_id: str
    imap_uid: str
    subject: str
    sender: str
    recipients: str
    cc: str
    received_at: Optional[datetime]
    body_text: str
    body_preview: str
    attachments: List[Attachment]


def subject_matches(subject: str, prefix: str) -> bool:
    """True when ``subject`` STARTS WITH ``prefix`` (case-insensitive).

    Leading whitespace is tolerated; anything else in front is not. So
    ``"JNPA Vessel Call"`` matches while ``"RE: JNPA Vessel Call"``,
    ``"FW: JNPA ..."`` and ``"ABC JNPA Data"`` do not — which is the stated
    requirement.
    """
    return (subject or "").strip().upper().startswith((prefix or "").strip().upper())


def _decode(raw: Optional[str]) -> str:
    """RFC-2047 header -> text, tolerant of malformed encodings."""
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw))).strip()
    except Exception:  # noqa: BLE001 — a bad header must not sink the whole poll
        return str(raw).strip()


def _body_text(msg: Message) -> str:
    """Best plain-text rendering. Prefers text/plain; falls back to stripped HTML."""
    plain, html = "", ""
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_filename():           # an attachment, not the body
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        if ctype == "text/plain" and not plain:
            plain = text
        elif ctype == "text/html" and not html:
            html = text
    if plain.strip():
        return plain.strip()
    if html.strip():
        import re
        return re.sub(r"<[^>]+>", " ", html)[:20000].strip()
    return ""


def _attachments(msg: Message, *, with_content: bool) -> List[Attachment]:
    out: List[Attachment] = []
    for part in msg.walk() if msg.is_multipart() else []:
        filename = part.get_filename()
        if not filename:
            continue
        try:
            payload = part.get_payload(decode=True) or b""
        except Exception:  # noqa: BLE001 — one corrupt part must not lose the rest
            payload = b""
        if len(payload) > _MAX_ATTACHMENT_BYTES:
            payload = b""
        out.append(Attachment(
            filename=_decode(filename) or "attachment",
            content_type=part.get_content_type() or "application/octet-stream",
            size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest() if payload else "",
            content=payload if with_content else None,
        ))
    return out


def _connect(cfg: MailboxConfig) -> imaplib.IMAP4:
    """Open and authenticate. Raises :class:`MailboxError` with a safe message.

    The provider's own error text is NOT propagated: Gmail's authentication
    failures can echo back the submitted username, so the user-facing sentence
    is written here and the raw error is left for the caller to log.
    """
    if not cfg.configured:
        raise MailboxError("not_configured",
                           "The mailbox is not configured. Set EMAIL_HOST, EMAIL_USER "
                           "and EMAIL_PASSWORD, then restart the gateway.")
    try:
        if cfg.security.upper() in ("SSL", "TLS", "IMAPS"):
            conn: imaplib.IMAP4 = imaplib.IMAP4_SSL(cfg.host, cfg.port, timeout=cfg.timeout_s)
        else:
            conn = imaplib.IMAP4(cfg.host, cfg.port, timeout=cfg.timeout_s)
            conn.starttls()
    except Exception as exc:  # noqa: BLE001
        raise MailboxError("connect_failed",
                           f"Could not reach the mail server at {cfg.host}:{cfg.port}.") from exc
    try:
        conn.login(cfg.user, cfg.password)
    except Exception as exc:  # noqa: BLE001
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass
        raise MailboxError("auth_failed",
                           "The mail server rejected the configured credentials. For Gmail, "
                           "EMAIL_PASSWORD must be a 16-character App Password and IMAP must "
                           "be enabled on the account.") from exc
    return conn


def _select_readonly(conn: imaplib.IMAP4, mailbox: str) -> None:
    typ, _ = conn.select(mailbox, readonly=True)
    if typ != "OK":
        raise MailboxError("mailbox_not_found", f"Mailbox {mailbox!r} could not be opened.")


def fetch_matching(cfg: MailboxConfig, *, with_content: bool = False) -> List[EmailSummary]:
    """Return the most recent messages whose subject starts with the prefix.

    Scans at most ``cfg.fetch_limit`` newest messages. Never mutates the mailbox.
    """
    conn = _connect(cfg)
    try:
        _select_readonly(conn, cfg.mailbox)
        typ, data = conn.uid("SEARCH", None, "ALL")
        if typ != "OK" or not data or not data[0]:
            return []
        uids = data[0].split()[-max(1, cfg.fetch_limit):]
        out: List[EmailSummary] = []
        for uid in reversed(uids):                     # newest first
            summary = _fetch_one(conn, uid, cfg, with_content=with_content)
            if summary is not None:
                out.append(summary)
        return out
    finally:
        _close(conn)


def fetch_by_uid(cfg: MailboxConfig, imap_uid: str,
                 *, with_content: bool = True) -> Optional[EmailSummary]:
    """Re-read ONE message (used to get attachment bytes at Process time).

    Attachment payloads are deliberately not persisted, so every preview/import
    re-reads them here.
    """
    conn = _connect(cfg)
    try:
        _select_readonly(conn, cfg.mailbox)
        return _fetch_one(conn, str(imap_uid).encode(), cfg, with_content=with_content)
    finally:
        _close(conn)


def _fetch_one(conn: imaplib.IMAP4, uid: bytes, cfg: MailboxConfig,
               *, with_content: bool) -> Optional[EmailSummary]:
    typ, payload = conn.uid("FETCH", uid, "(RFC822)")
    if typ != "OK" or not payload or not isinstance(payload[0], tuple):
        return None
    msg = email.message_from_bytes(payload[0][1])
    subject = _decode(msg.get("Subject"))
    if not subject_matches(subject, cfg.subject_prefix):
        return None

    received: Optional[datetime] = None
    try:
        raw_date = msg.get("Date")
        if raw_date:
            received = parsedate_to_datetime(raw_date)
            if received is not None and received.tzinfo is None:
                received = received.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001 — an unparseable Date must not drop the mail
        received = None

    body = _body_text(msg)
    uid_s = uid.decode() if isinstance(uid, bytes) else str(uid)
    # A message with no Message-ID still needs a stable identity for the UNIQUE
    # ledger key; derive one from the mailbox + uid rather than inventing a random
    # value, so re-polling maps to the same row.
    message_id = _decode(msg.get("Message-ID")) or f"<no-id-{cfg.mailbox}-{uid_s}>"
    return EmailSummary(
        message_id=message_id,
        imap_uid=uid_s,
        subject=subject,
        sender=_decode(msg.get("From")),
        recipients=_decode(msg.get("To")),
        cc=_decode(msg.get("Cc")),
        received_at=received,
        body_text=body,
        body_preview=(body[:_PREVIEW_CHARS] + ("…" if len(body) > _PREVIEW_CHARS else "")),
        attachments=_attachments(msg, with_content=with_content),
    )


def _close(conn: imaplib.IMAP4) -> None:
    for step in (conn.close, conn.logout):
        try:
            step()
        except Exception:  # noqa: BLE001 — teardown is best-effort
            pass


def check_connection(cfg: MailboxConfig) -> Tuple[bool, str]:
    """Health probe for the page banner. Never raises, never leaks credentials."""
    try:
        conn = _connect(cfg)
    except MailboxError as exc:
        return False, str(exc)
    try:
        _select_readonly(conn, cfg.mailbox)
        return True, "Connected."
    except MailboxError as exc:
        return False, str(exc)
    finally:
        _close(conn)


__all__ = ["Attachment", "EmailSummary", "MailboxError", "check_connection",
           "fetch_by_uid", "fetch_matching", "subject_matches"]
