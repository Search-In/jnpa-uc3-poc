"""Mailbox configuration — environment only, never source, never the wire.

Mirrors the posture of ``gateway/mailer.py`` (the OUTBOUND seam): every value is
read from ``os.environ`` at call time, nothing is committed, and the feature is
OFF unless explicitly configured. mailer.py sends over SMTP; this module is the
INBOUND counterpart and speaks IMAP — the two are deliberately separate concerns
and share no state.

SECURITY CONTRACT
    * :attr:`MailboxConfig.password` is the ONLY carrier of the app password. It
      is excluded from ``repr``/``str`` (``repr=False`` on the field) so it cannot
      reach a log line, traceback frame render, or f-string by accident.
    * :meth:`MailboxConfig.public` is the ONLY shape that may cross an API
      boundary. It contains no password field at all — this is enforced by a test
      rather than by convention.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


def _as_bool(raw: Optional[str], default: bool = False) -> bool:
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(raw: Optional[str], default: int) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


@dataclass
class MailboxConfig:
    """Resolved IMAP settings. Build with :func:`from_env`."""

    host: str = ""
    port: int = 993
    user: str = ""
    # repr=False keeps the secret out of every automatic rendering of this object
    # (logging an exception that holds a config, `print(cfg)`, pytest assertion
    # diffs, structlog value coercion, ...).
    password: str = field(default="", repr=False)
    security: str = "SSL"
    mailbox: str = "INBOX"
    #: Subject prefix an email must START WITH to be listed (case-insensitive).
    subject_prefix: str = "JNPA"
    #: Most recent N messages scanned per poll — bounds both time and memory.
    fetch_limit: int = 100
    timeout_s: int = 20
    enabled: bool = False

    @property
    def configured(self) -> bool:
        """True when enough is set to attempt a connection."""
        return bool(self.enabled and self.host and self.user and self.password)

    def public(self) -> Dict[str, Any]:
        """Credential-free view. The ONLY form that may be returned by an API.

        Deliberately has no password key — not a masked one. A masked field is
        still a field, and masking is one refactor away from leaking.
        """
        return {
            "host": self.host,
            "port": self.port,
            "user": _mask_user(self.user),
            "security": self.security,
            "mailbox": self.mailbox,
            "subject_prefix": self.subject_prefix,
            "enabled": self.enabled,
            "configured": self.configured,
        }


def _mask_user(user: str) -> str:
    """``ops.admin@jnpa.example`` -> ``o*******n@jnpa.example``.

    The mailbox address is not a secret, but it is a spam/phishing target and
    there is no operational reason for the browser to hold it verbatim.
    """
    if "@" not in user:
        return "***" if user else ""
    local, _, domain = user.partition("@")
    if len(local) <= 2:
        return f"{'*' * len(local)}@{domain}"
    return f"{local[0]}{'*' * (len(local) - 2)}{local[-1]}@{domain}"


def from_env() -> MailboxConfig:
    """Read the mailbox settings from the environment.

    Names follow the requirement (EMAIL_HOST / EMAIL_PORT / EMAIL_USER /
    EMAIL_PASSWORD / EMAIL_SECURITY) and are declared in .env.local.example and
    .env.aws.example. Absent config yields ``configured=False`` and every caller
    degrades to "mailbox not configured" rather than raising.
    """
    host = os.environ.get("EMAIL_HOST", "").strip()
    user = os.environ.get("EMAIL_USER", "").strip()
    password = os.environ.get("EMAIL_PASSWORD", "")
    # Default ON when a host is present so configuring the mailbox is enough to
    # enable it, but an explicit EMAIL_INGEST_ENABLED=false always wins.
    enabled = _as_bool(os.environ.get("EMAIL_INGEST_ENABLED"), bool(host))
    return MailboxConfig(
        host=host,
        port=_as_int(os.environ.get("EMAIL_PORT"), 993),
        user=user,
        password=password,
        security=(os.environ.get("EMAIL_SECURITY", "SSL").strip().upper() or "SSL"),
        mailbox=(os.environ.get("EMAIL_MAILBOX", "INBOX").strip() or "INBOX"),
        subject_prefix=(os.environ.get("EMAIL_SUBJECT_PREFIX", "JNPA").strip() or "JNPA"),
        fetch_limit=_as_int(os.environ.get("EMAIL_FETCH_LIMIT"), 100),
        timeout_s=_as_int(os.environ.get("EMAIL_TIMEOUT_S"), 20),
        enabled=enabled,
    )


__all__ = ["MailboxConfig", "from_env"]
