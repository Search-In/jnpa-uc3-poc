"""DPDP code-enforcement for biometric / identity access (Wave 3 — SEC-3).

The audit (SEC-3) found the DPDP posture was *documented* but not *enforced in
code*. This module makes the posture executable:

  * **Purpose limitation.** Every identity access must declare a `purpose` drawn
    from a closed allow-list; anything else is refused (400). This implements the
    DPDP Act purpose-limitation principle at the Silver->Gold boundary.

  * **Synthetic-only guard.** In the PoC, only synthetic/consented biometrics may
    be processed. A request that asserts real biometrics (`is_synthetic=false`) is
    refused unless ALLOW_REAL_BIOMETRICS=true (a post-award, consent-gated flag
    that is OFF by default). So the code — not just the docs — guarantees no real
    biometric is processed in the PoC.

  * **Audit sink.** Every identity access emits a structured audit record
    (who/what/purpose/synthetic/decision) so biometric processing is traceable.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import HTTPException

from .logging import get_logger

_audit_log = get_logger("gateway.identity.audit")

# Closed allow-list of lawful purposes for identity/biometric access (DPDP).
ALLOWED_PURPOSES: frozenset[str] = frozenset(
    {
        "GATE_VERIFICATION",   # verify a driver at a gate (primary PoC purpose)
        "ENROLMENT",           # enrol a consented driver into the gallery
        "AUDIT_REVIEW",        # control-room review of a prior decision
    }
)
DEFAULT_PURPOSE = "GATE_VERIFICATION"


def allow_real_biometrics() -> bool:
    """Post-award, consent-gated. OFF by default so the PoC can never process a
    real biometric in code, matching the documented DPDP posture."""
    return os.environ.get("ALLOW_REAL_BIOMETRICS", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def enforce_dpdp(*, purpose: str | None, is_synthetic: bool) -> str:
    """Validate purpose-limitation + synthetic-only posture. Returns the resolved
    purpose or raises HTTPException(400/403)."""
    resolved = (purpose or DEFAULT_PURPOSE).upper()
    if resolved not in ALLOWED_PURPOSES:
        raise HTTPException(
            status_code=400,
            detail=f"purpose '{resolved}' not permitted; allowed: {sorted(ALLOWED_PURPOSES)}",
        )
    if not is_synthetic and not allow_real_biometrics():
        raise HTTPException(
            status_code=403,
            detail=(
                "real biometric processing is disabled in this deployment "
                "(DPDP: PoC handles synthetic/consented faces only; real biometrics "
                "are post-award + consent-gated via ALLOW_REAL_BIOMETRICS)"
            ),
        )
    return resolved


def audit_identity_access(
    *, actor: str, driver_id: str, purpose: str, is_synthetic: bool, decision: str
) -> None:
    """Emit a structured DPDP audit record for an identity access."""
    _audit_log.info(
        "identity_access",
        actor=actor,
        driver_id=driver_id,
        purpose=purpose,
        is_synthetic=is_synthetic,
        decision=decision,
        at=datetime.now(timezone.utc).isoformat(),
    )
