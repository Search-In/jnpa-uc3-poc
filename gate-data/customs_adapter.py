"""ICEGATE Customs Adapter (Phase 4) — feeds the ICEGATE gate source from the REAL
customs document tables (``jnpa.customs_*``, module 5) instead of the seed simulator.

Behind a feature flag (``GATE_ICEGATE_ADAPTER=customs``), the gate-data service serves
ICEGATE captures transformed from the imported CHPOI messages: every IGM-declared
container becomes an ICEGATE gate capture, with LEO / assessment derived from OOC
(out-of-charge) and RMS (scanning selection). The response DTO (``GateCapture``) and the
``/api/gate-data`` contract are UNCHANGED — only the ICEGATE data source flips to LIVE.
e-Seal / Form-13 / Weighbridge remain SIM (module 8, out of scope this phase).

Rollback is SYMMETRIC and instant: unset ``GATE_ICEGATE_ADAPTER`` → on the next gate-data
boot the LIVE ICEGATE rows are purged (:func:`purge_live_icegate`) and the synthetic seed
ICEGATE is restored — no stale LIVE rows remain, no code change, no UI change.

Design:
  * :func:`sync_icegate_captures` runs the purge-synthetic + insert-real transform in a
    SINGLE transaction (atomic): if the insert fails the whole thing rolls back and ICEGATE
    keeps its prior state — it is never left empty. Idempotent via ON CONFLICT DO NOTHING.
  * :func:`purge_live_icegate` is the symmetric rollback used when the flag is off.
Both touch ONLY ICEGATE rows; e-Seal / Form-13 / Weighbridge rows are never affected.
"""
from __future__ import annotations

import time
import os
from typing import Optional

from jnpa_shared.logging import get_logger

log = get_logger("gate_data.customs_adapter")

# Feature flag values that turn the adapter ON (default OFF → seed/SIM behaviour).
_TRUTHY = {"customs", "1", "true", "yes", "on", "live"}

# Env flag name (per-source, in the GATE_ICEGATE_* namespace like the other providers).
FLAG = "GATE_ICEGATE_ADAPTER"


def enabled() -> bool:
    """True when the ICEGATE customs adapter is switched on. Default False."""
    return os.environ.get(FLAG, "").strip().lower() in _TRUTHY


# Transform: customs IGM containers → ICEGATE gate captures, in the GateCapture shape.
#   container_no       ← customs_igm_container.container_no  (real declared container)
#   igm_no             ← customs_igm_container.igm_no
#   status/leo_status  ← 'GRANTED' when an OOC (out-of-charge) exists for the box, else 'PENDING'
#   assessment         ← 'ASSESSED' when RMS selected the box for scanning, else 'FACILITATED'
#   shipping_bill_no   ← the OOC Bill-of-Entry number (import declaration), when present
#   captured_at        ← COALESCE(entry-inward, ETA, message sent, message created) — never NULL
# The payload keys mirror gate_data.icegate_sim.icegate_message() for shape fidelity.
_INSERT_SQL = """
INSERT INTO jnpa.gate_captures
    (capture_type, container_no, vehicle_plate, gate_id, source_mode, status, captured_at, payload)
SELECT
    'ICEGATE',
    c.container_no,
    NULL,
    NULL,
    'live',
    CASE WHEN vcs.ooc_cleared THEN 'GRANTED' ELSE 'PENDING' END,
    COALESCE(v.entry_inward, v.expected_arrival, m.sent_ts, m.created_at),
    jsonb_build_object(
        'shipping_bill_no', ooc.bill_of_entry_no,
        'container_no',     c.container_no,
        'leo_status',       CASE WHEN vcs.ooc_cleared THEN 'GRANTED' ELSE 'PENDING' END,
        'leo_granted',      COALESCE(vcs.ooc_cleared, false),
        'igm_no',           c.igm_no,
        'assessment',       CASE WHEN vcs.rms_selected THEN 'ASSESSED' ELSE 'FACILITATED' END,
        'source',           'ICEGATE',
        'origin',           'customs-adapter'
    )
FROM jnpa.customs_igm_container c
JOIN jnpa.customs_igm_cargo_line l ON l.id = c.cargo_line_id
JOIN jnpa.customs_igm_vessel     v ON v.id = l.vessel_id
JOIN jnpa.customs_messages       m ON m.id = v.message_id
LEFT JOIN jnpa.v_customs_container_status vcs ON vcs.container_no = c.container_no
LEFT JOIN LATERAL (
    SELECT oc.bill_of_entry_no
    FROM jnpa.customs_ooc_container oc
    WHERE oc.container_no = c.container_no
    LIMIT 1
) ooc ON true
ON CONFLICT (container_no, capture_type, captured_at) DO NOTHING
"""

# Purge synthetic (seed) ICEGATE rows — runs INSIDE the atomic sync so the ICEGATE tab
# shows ONLY real data when the adapter is active. Touches ICEGATE/sim rows only.
_PURGE_SIM_SQL = (
    "DELETE FROM jnpa.gate_captures "
    "WHERE capture_type = 'ICEGATE' AND source_mode = 'sim'"
)

# Symmetric-rollback purge — removes previously-synced LIVE ICEGATE rows when the adapter
# is disabled. Touches ICEGATE/live rows only.
_PURGE_LIVE_SQL = (
    "DELETE FROM jnpa.gate_captures "
    "WHERE capture_type = 'ICEGATE' AND source_mode = 'live'"
)

# Candidate count (every IGM container yields one ICEGATE capture; FKs guarantee the join
# parents exist), used only to log rows-skipped (= candidates - inserted, the dedup count).
_COUNT_CANDIDATES_SQL = "SELECT count(*) FROM jnpa.customs_igm_container"


def _rowcount(result) -> int:
    rc = result.rowcount
    return rc if rc is not None and rc > 0 else 0


async def sync_icegate_captures(dsn: Optional[str]) -> int:
    """Atomically materialise real ICEGATE captures from the customs tables into
    ``jnpa.gate_captures``. The purge-synthetic + insert-real transform runs in a SINGLE
    transaction: on any error the whole thing rolls back and ICEGATE keeps its prior state
    (never left empty). Idempotent (ON CONFLICT DO NOTHING); returns rows inserted.
    Best-effort: an error is logged and swallowed so it can never abort gate-data boot."""
    if not dsn:
        return 0
    from sqlalchemy import text

    from jnpa_shared.db import get_engine

    t0 = time.perf_counter()
    log.info("customs_sync_start", msg="Starting Customs sync")
    try:
        async with get_engine(dsn).begin() as conn:      # one atomic transaction
            # Count the source IGM containers FIRST so 'fetched' is logged even when it is
            # zero — that distinguishes "no source data to convert" (fetched=0, an import
            # problem) from "source present but insert failed" (fetched>0 then an error).
            fetched = int((await conn.execute(text(_COUNT_CANDIDATES_SQL))).scalar() or 0)
            log.info("customs_sync_fetched", records=fetched,
                     msg=f"Fetched {fetched} customs records")
            await conn.execute(text(_PURGE_SIM_SQL))
            inserted = _rowcount(await conn.execute(text(_INSERT_SQL)))
        skipped = max(0, fetched - inserted)
        duration_ms = round((time.perf_counter() - t0) * 1000, 1)
        log.info("customs_sync_inserted", captures=inserted,
                 msg=f"Inserted {inserted} ICEGATE captures")
        log.info("customs_sync_skipped", duplicates=skipped,
                 msg=f"Skipped {skipped} duplicates")
        log.info("customs_sync_complete", fetched=fetched, inserted=inserted,
                 skipped=skipped, duration_ms=duration_ms, atomic=True,
                 msg="Completed Customs sync")
        return inserted
    except Exception as exc:  # noqa: BLE001 — never break boot; the transaction already rolled back
        # Surface the real failure loudly (previously swallowed at warning): a missing
        # object/column would otherwise leave ICEGATE silently empty despite LIVE.
        log.error("customs_sync_failed", error=str(exc), error_type=type(exc).__name__,
                  duration_ms=round((time.perf_counter() - t0) * 1000, 1),
                  msg="Customs sync failed; ICEGATE left unchanged")
        return 0


async def purge_live_icegate(dsn: Optional[str]) -> int:
    """Symmetric rollback: remove previously-synced LIVE ICEGATE rows so no stale real
    rows remain after the adapter is disabled (the caller re-seeds the synthetic ICEGATE).
    Returns rows removed. Best-effort; logged."""
    if not dsn:
        return 0
    from sqlalchemy import text

    from jnpa_shared.db import get_engine

    t0 = time.perf_counter()
    try:
        async with get_engine(dsn).begin() as conn:
            removed = _rowcount(await conn.execute(text(_PURGE_LIVE_SQL)))
        log.info("customs_adapter_rollback", rows_removed=removed,
                 duration_ms=round((time.perf_counter() - t0) * 1000, 1))
        return removed
    except Exception as exc:  # noqa: BLE001
        log.warning("customs_adapter_rollback_failed", error=str(exc))
        return 0


__all__ = ["enabled", "sync_icegate_captures", "purge_live_icegate", "FLAG"]
