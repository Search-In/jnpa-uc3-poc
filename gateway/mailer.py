"""Email advisory channel — admin notification for NEW congestion alerts.

Structurally identical to ``gateway/sms.py``: a small ``EmailProvider`` interface
with an env-gated **no-op default**, so email fans out alongside the WebSocket /
WebPush / FCM channels and a real provider (SMTP relay, SES, SendGrid) is one env
var + one adapter away — no code change to the call sites.

    EMAIL_PROVIDER=none      (default)  -> NoopEmailProvider  (records intent, sends nothing)
    EMAIL_PROVIDER=log                  -> LogEmailProvider   (audit-logs the message)
    EMAIL_PROVIDER=smtp                 -> SmtpEmailProvider  (stdlib smtplib, no new dependency)
    EMAIL_PROVIDER=<your impl>          -> register a real provider in _build_provider()

Recipients come from ``ADMIN_ALERT_EMAILS`` (comma-separated) — no address is ever
hardcoded. With no recipients configured nothing is sent, no delivery row is
written and the application behaves exactly as it did before this module existed.

Delivery is best-effort and never raises into the caller: a provider error is
caught and reported as not-delivered, exactly like the SMS and WebPush channels.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, Tuple

from .logging import get_logger

log = get_logger("gateway.mailer")


@dataclass
class EmailResult:
    delivered: bool
    provider: str
    to: str
    detail: str = ""


class EmailProvider(Protocol):
    name: str

    def send(self, to: str, subject: str, body: str) -> EmailResult: ...


@dataclass
class NoopEmailProvider:
    """Default. Records that an email *would* be sent (so the audit trail shows the
    channel firing) but sends nothing — zero external dependency, zero credentials."""

    name: str = "none"

    def send(self, to: str, subject: str, body: str) -> EmailResult:
        log.debug("email_noop", to=to, subject=subject)
        return EmailResult(delivered=False, provider=self.name, to=to,
                           detail="noop (no provider configured)")


@dataclass
class LogEmailProvider:
    """Audit-logs the message as if sent. Useful for demos/e2e without an SMTP
    account; swap for a real provider in production."""

    name: str = "log"

    def send(self, to: str, subject: str, body: str) -> EmailResult:
        log.info("email_send", provider=self.name, to=to, subject=subject, body=body)
        return EmailResult(delivered=True, provider=self.name, to=to, detail="logged")


@dataclass
class SmtpEmailProvider:
    """Real delivery over an SMTP relay using the stdlib only (no new dependency).

    Configured by SMTP_HOST / SMTP_PORT / SMTP_USERNAME / SMTP_PASSWORD /
    SMTP_STARTTLS / EMAIL_FROM. Selected only when SMTP_HOST is set — otherwise
    ``_build_provider`` falls back to the safe no-op.
    """

    host: str
    port: int = 587
    username: str = ""
    password: str = ""
    starttls: bool = True
    sender: str = ""
    name: str = "smtp"

    def send(self, to: str, subject: str, body: str) -> EmailResult:
        import smtplib
        from email.message import EmailMessage  # stdlib; this module is `mailer`, not `email`

        msg = EmailMessage()
        msg["From"] = self.sender or (self.username or f"no-reply@{self.host}")
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        with smtplib.SMTP(self.host, self.port, timeout=10) as smtp:
            if self.starttls:
                smtp.starttls()
            if self.username:
                smtp.login(self.username, self.password)
            smtp.send_message(msg)
        log.info("email_send", provider=self.name, to=to, subject=subject)
        return EmailResult(delivered=True, provider=self.name, to=to, detail="smtp accepted")


def _as_bool(raw: Optional[str], default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# Register real providers here (e.g. "ses": SesEmailProvider). Kept as one function
# so adding one is a localized change with no edits to the call sites.
def _build_provider() -> EmailProvider:
    name = os.environ.get("EMAIL_PROVIDER", "none").strip().lower()
    if name == "log":
        return LogEmailProvider()
    if name == "smtp":
        host = os.environ.get("SMTP_HOST", "").strip()
        if host:  # unconfigured SMTP must not become a hard dependency
            return SmtpEmailProvider(
                host=host,
                port=int(os.environ.get("SMTP_PORT", "587") or 587),
                username=os.environ.get("SMTP_USERNAME", "").strip(),
                password=os.environ.get("SMTP_PASSWORD", ""),
                starttls=_as_bool(os.environ.get("SMTP_STARTTLS"), True),
                sender=os.environ.get("EMAIL_FROM", "").strip(),
            )
    # "none" / unknown / unconfigured -> safe no-op default.
    return NoopEmailProvider()


_provider: EmailProvider | None = None


def get_provider() -> EmailProvider:
    global _provider
    if _provider is None:
        _provider = _build_provider()
    return _provider


def reset_provider() -> None:
    """Test hook: force re-read of EMAIL_PROVIDER on next get_provider()."""
    global _provider
    _provider = None


def admin_recipients() -> List[str]:
    """Configured admin address(es) from ADMIN_ALERT_EMAILS (comma-separated).

    Never hardcoded: unset/blank means "email not configured" and every caller
    below turns into a no-op.
    """
    raw = os.environ.get("ADMIN_ALERT_EMAILS", "")
    return [a.strip() for a in raw.split(",") if a.strip()]


def send_email(to: str, subject: str, body: str) -> EmailResult:
    """Send one advisory email. Never raises — mirrors the SMS/WebPush channels."""
    if not to:
        return EmailResult(delivered=False, provider=get_provider().name, to="",
                           detail="no recipient")
    try:
        return get_provider().send(to, subject, body)
    except Exception as exc:  # noqa: BLE001
        log.warning("email_send_failed", to=to, error=str(exc))
        return EmailResult(delivered=False, provider=get_provider().name, to=to,
                           detail=f"error: {exc}")


def congestion_alert_to_email(alert_id: str, payload: Dict[str, Any]) -> Tuple[str, str]:
    """Render an already-created TRAFFIC_CONGESTION alert into (subject, body).

    Read-only over the alert payload the congestion service already builds — no
    severity, score or threshold logic lives here.
    """
    severity = payload.get("severity") or "MEDIUM"
    segment = payload.get("segment_id") or "unknown segment"
    subject = f"[JNPA UC-3] {severity} congestion alert — {segment}"
    lines = [
        f"Severity        : {severity}",
        f"Segment         : {segment}",
        f"Route           : {payload.get('route') or '-'}",
        f"Gate            : {payload.get('gate') or '-'}",
        f"Congestion score: {payload.get('score')}",
        f"Recommended     : {payload.get('recommended_action') or '-'}",
        f"Alert id        : {alert_id}",
    ]
    return subject, "\n".join(lines)


async def notify_congestion_alert(alert_id: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Injected into ``services.congestion_alert.raise_congestion_alerts``.

    Called once per NEW, successfully-deduplicated congestion alert. Returns None
    when no admin recipient is configured (nothing sent, no delivery row written,
    behaviour identical to before), otherwise a delivery summary the service
    records on the existing ``core.notification`` trail.
    """
    recipients = admin_recipients()
    if not recipients:
        return None
    subject, body = congestion_alert_to_email(alert_id, payload)
    results = [
        # Providers are blocking (smtplib): keep them off the event loop.
        await asyncio.to_thread(send_email, to, subject, body)
        for to in recipients
    ]
    return {
        "delivered": any(r.delivered for r in results),
        "recipients": recipients,
        "provider": get_provider().name,
        "detail": "; ".join(f"{r.to}: {r.detail}" for r in results)[:500],
    }


__all__ = [
    "EmailResult", "EmailProvider", "NoopEmailProvider", "LogEmailProvider",
    "SmtpEmailProvider", "get_provider", "reset_provider", "admin_recipients",
    "send_email", "congestion_alert_to_email", "notify_congestion_alert",
]
