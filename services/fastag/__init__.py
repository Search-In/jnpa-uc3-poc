"""FASTag layer over the granted ULIP APIs — strict mappers + persistence.

Two responsibilities, kept apart on purpose:

* :mod:`services.fastag.mappers` — the ONLY place that turns a raw vendor
  response into a validated DTO + a DB-ready dict. No HTTP lives here.
* :mod:`services.fastag.service` — the ONLY place that writes to the DB, with
  the right idempotency strategy per API. No transformation lives here.

Transport is NOT ours: every ULIP call goes through the shared
:class:`integrations.ulip.UlipClient`, so the login token, retry budget,
redaction rules and audit shape are the same ones /api/logistics, /api/ldb and
/api/vahan use. The former ``services.fastag.ulip_client`` (its own base URL,
its own auth, its own retries, never configured against a real endpoint) is no
longer on any request path; it is left in the tree solely because
``services.fastag.validation.live_validation`` — the standalone vendor-contract
harness — still drives it, and is no longer re-exported here.

Which ULIP APIs back which surface:

    /api/fastag/transactions   FASTAG/01  (72-hour retention — see poller)
    /api/fastag/tag-status     FASTAG/02
    /api/fastag/toll-enroute   GATISHAKTI/04 registry (no ULIP route API)
    /api/fastag/balance        none granted — replays a stored snapshot only

The DTOs themselves are defined once in :mod:`jnpa_shared.fastag`.
"""

from .mappers import (
    map_fastag_balance,
    map_fastag_tag_status,
    map_fastag_transactions,
    map_toll_enroute,
)
from .poller import FastagPoller
from .service import FastagService

__all__ = [
    "map_toll_enroute",
    "map_fastag_balance",
    "map_fastag_transactions",
    "map_fastag_tag_status",
    "FastagService",
    "FastagPoller",
]
