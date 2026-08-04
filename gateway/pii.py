"""Request-scoped PII entitlement — decides WHO sees cleartext, then delegates.

Split from ``jnpa_shared.pii`` (which owns the framework-free masking primitives)
so the "may this caller see it?" question lives next to the auth layer that
answers it, and the "how is it masked?" question stays unit-testable on its own.

Usage in a router::

    from ..pii import mask_for_request

    @router.get("")
    async def list_drivers(request: Request, ...):
        res = await service.list_drivers(...)
        return mask_for_request(request, res)

Entitlement rule (fail closed at every step):

  1. Masking globally disabled (``PII_MASKING_ENABLED=false``)  -> cleartext.
  2. No principal on the request                                -> MASKED.
     This is the important one: with ``AUTH_ENABLED=false`` the middleware never
     attaches a principal, so an unauthenticated demo build masks by default
     rather than serving 31.8k licence numbers to anyone who can reach the port.
  3. Principal's role in ``PII_UNMASK_ROLES``                   -> cleartext.
  4. Anything else                                              -> MASKED.

Every cleartext disclosure is logged (role + sub + surface) so there is an audit
trail of who de-anonymised what.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from starlette.requests import Request

from jnpa_shared.pii import mask_payload, masking_enabled, unmask_roles

from .logging import get_logger

log = get_logger("gateway.pii")


def principal_of(request: Request) -> Optional[Any]:
    """The auth middleware's principal, or None when auth is off / public path."""
    return getattr(request.state, "principal", None)


def may_view_pii(request: Request) -> bool:
    """True only when this caller is entitled to cleartext PII (see module doc)."""
    if not masking_enabled():
        return True
    principal = principal_of(request)
    if principal is None:
        # No proven identity -> no cleartext. Covers AUTH_ENABLED=false.
        return False
    role = getattr(principal, "role", None)
    return bool(role) and role in unmask_roles()


def mask_for_request(request: Request, payload: Any, *,
                     surface: str = "", extra_fields: Iterable[str] = ()) -> Any:
    """Mask ``payload`` unless the request's principal is entitled to cleartext.

    Returns the payload unchanged (same object) when the caller is entitled, and
    a masked DEEP COPY otherwise — the input is never mutated, so a row cached
    and shared across callers cannot be corrupted by serving order.
    """
    if may_view_pii(request):
        principal = principal_of(request)
        if principal is not None:
            # Audit trail for every de-anonymised read.
            log.info(
                "pii_disclosed",
                surface=surface or request.url.path,
                role=getattr(principal, "role", None),
                sub=getattr(principal, "sub", None),
            )
        return payload
    return mask_payload(payload, extra_fields=extra_fields)


__all__ = ["may_view_pii", "mask_for_request", "principal_of"]
