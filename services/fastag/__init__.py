"""FASTag ULIP integration layer — pure transport + strict mappers.

Two responsibilities, kept apart on purpose:

* :mod:`services.fastag.ulip_client` — the ONLY place that speaks HTTP to the
  ULIP vendor. No transformation logic lives here.
* :mod:`services.fastag.mappers` — the ONLY place that turns a raw vendor
  response into a validated DTO + a DB-ready dict. No HTTP lives here.

The DTOs themselves are defined once in :mod:`jnpa_shared.fastag`.
"""

from .ulip_client import UlipClientError, UlipFastagClient
from .mappers import (
    map_fastag_balance,
    map_fastag_transactions,
    map_toll_enroute,
)
from .service import FastagService

__all__ = [
    "UlipFastagClient",
    "UlipClientError",
    "map_toll_enroute",
    "map_fastag_balance",
    "map_fastag_transactions",
    "FastagService",
]
