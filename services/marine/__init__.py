"""UC-I Marine service package — raw-SQL repository + read orchestration.

The vessel-call spine for Use Case I (Vessel Traffic Management & Optimisation):
one row per vessel visit (``core.vessel_call``) plus its fine-grained actuals
(``core.vessel_call_event``), sourced from the NLP-Marine PCS message family, the
per-terminal berthing reports, the pilot cards and TOS File 02.

Owns ONLY the ``core.*`` UC-I objects created by
infra/postgres/migrations/0038_marine_vessel_call.sql. Additive + read-only wrt
every existing table: it never reads or writes anything in the ``jnpa`` schema, so
the berthing module and every other UC3 module are entirely unaffected. A later
migration adds a non-breaking soft link (jnpa.berthing_reports.id ->
core.vessel_call.call_id BY VALUE, no FK).

Layering mirrors :mod:`services.berthing`:

* :class:`VesselCallRepository` — the ONLY place that speaks SQL (raw ``text()``).
* :class:`VesselCallService`    — read orchestration (list / lookup / timeline / stats).

Deliberately NOT here yet (later slices): the PCS/pilot-card parsers, the
validate → preview → import upload sub-module, and the vessel / pilot / port-craft /
sea-channel / bathymetry modules that will join this package under the same
``core`` schema.
"""

from .repository import VesselCallRepository
from .service import VesselCallService
from .upload_service import MarineUploadService

__all__ = ["VesselCallRepository", "VesselCallService", "MarineUploadService"]
