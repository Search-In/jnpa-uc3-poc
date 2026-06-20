"""SMS advisory channel (Wave 4 — APP-3 / SCOPE-IU2).

The tender names SMS twice (Appendix C intended-use IU2 and bid §8.5). The audit
found no SMS path at all. This module adds the **provider seam**: a small
`SmsProvider` interface with an env-gated **no-op default**, so SMS advisories fan
out alongside the WebPush channel and a real provider (Twilio / AWS SNS / MSG91 /
Gupshup) is one env var + one adapter away — no code change to the call sites.

    SMS_PROVIDER=none      (default)  -> NoopSmsProvider  (records intent, sends nothing)
    SMS_PROVIDER=log                  -> LogSmsProvider   (audit-logs the message)
    SMS_PROVIDER=<your impl>          -> register a real provider in PROVIDERS

Delivery is best-effort and never raises into the request path: a provider error
is caught and reported as not-delivered, exactly like the WebPush channel.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Protocol

from .logging import get_logger

log = get_logger("gateway.sms")


@dataclass
class SmsResult:
    delivered: bool
    provider: str
    to: str
    detail: str = ""


class SmsProvider(Protocol):
    name: str

    def send(self, to: str, message: str) -> SmsResult: ...


@dataclass
class NoopSmsProvider:
    """Default. Records that an SMS *would* be sent (so the demo shows the channel
    firing) but sends nothing — zero external dependency, zero credentials."""

    name: str = "none"

    def send(self, to: str, message: str) -> SmsResult:
        log.debug("sms_noop", to=to, len=len(message))
        return SmsResult(delivered=False, provider=self.name, to=to, detail="noop (no provider configured)")


@dataclass
class LogSmsProvider:
    """Audit-logs the message as if sent. Useful for demos/e2e without a gateway
    account; swap for a real provider in production."""

    name: str = "log"

    def send(self, to: str, message: str) -> SmsResult:
        log.info("sms_send", provider=self.name, to=to, message=message)
        return SmsResult(delivered=True, provider=self.name, to=to, detail="logged")


# Register real providers here (e.g. "twilio": TwilioSmsProvider). Kept as a dict
# so adding one is a localized change with no edits to the call sites.
def _build_provider() -> SmsProvider:
    name = os.environ.get("SMS_PROVIDER", "none").strip().lower()
    if name == "log":
        return LogSmsProvider()
    # "none" / unknown -> safe no-op default.
    return NoopSmsProvider()


_provider: SmsProvider | None = None


def get_provider() -> SmsProvider:
    global _provider
    if _provider is None:
        _provider = _build_provider()
    return _provider


def reset_provider() -> None:
    """Test hook: force re-read of SMS_PROVIDER on next get_provider()."""
    global _provider
    _provider = None


def send_sms(to: str, message: str) -> SmsResult:
    """Fan an advisory out over SMS. Never raises — mirrors WebPush best-effort."""
    if not to:
        return SmsResult(delivered=False, provider=get_provider().name, to="", detail="no phone number")
    try:
        return get_provider().send(to, message)
    except Exception as exc:  # noqa: BLE001
        log.warning("sms_send_failed", to=to, error=str(exc))
        return SmsResult(delivered=False, provider=get_provider().name, to=to, detail=f"error: {exc}")


def advisory_to_sms_text(advisory: dict) -> str:
    """Render a reroute/alert advisory payload into a concise SMS body."""
    title = advisory.get("title") or advisory.get("kind") or "JNPA advisory"
    body = advisory.get("message") or advisory.get("detail") or ""
    gate = advisory.get("gate_id")
    eta = advisory.get("eta_min")
    parts = [str(title)]
    if body:
        parts.append(str(body))
    if gate:
        parts.append(f"Gate {gate}")
    if eta is not None:
        parts.append(f"ETA {eta} min")
    return " — ".join(parts)[:320]  # keep within a couple of SMS segments
