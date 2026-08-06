"""Read-only aggregate SQL for the what-if layer.

Same shape as :class:`services.cargo.repository.CargoRepository` — raw
parameterised SQL over the cached async engine, no ORM — with one hard extra
rule: **this repository may only read.**

That rule is enforced, not documented. Every statement goes through
:meth:`SimulationRepository._read`, which refuses anything that is not a
``SELECT`` / ``WITH`` and only ever opens ``engine.connect()`` (never
``engine.begin()``), so there is no transaction to commit even if a statement
slipped through. A what-if answer must be reproducible on demand and must never
leave the demo database in a different state than it found it.

Each read returns ``(rows, QueryTrace)``. The trace carries the SQL and its bound
parameters verbatim so the scenario can put them in the response — Notice §1.d
asks for "the API queries used to obtain the underlying data, so the working can
be traced", and a scenario cannot honour that if the SQL never leaves this layer.

Every read is also FAIL-SOFT: an absent table (a database not yet migrated to
0128-0131) or an unreachable Postgres yields ``[]`` plus a trace whose
``row_count`` is 0. The scenario then reports ``data_available: false`` and names
what was missing. It never substitutes a plausible number.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Mapping, Optional

from sqlalchemy import text

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

from .base import QueryTrace

log = get_logger("services.cargo.simulation.repository")

# A statement may start with SELECT or WITH (CTE) and nothing else. Checked after
# stripping leading comments/whitespace.
_READ_ONLY = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
# Belt and braces: no write verb may appear anywhere in a simulation statement,
# even inside a CTE body (`WITH x AS (DELETE ... RETURNING *)` is legal Postgres).
_WRITE_VERB = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|"
    r"merge|copy|vacuum|refresh)\b", re.IGNORECASE)


class SimulationWriteAttempt(RuntimeError):
    """Raised when a non-SELECT statement reaches the simulation repository.

    A programming error, never a user-facing one: the what-if layer is read-only
    by contract and this is the guard that keeps it that way."""


class SimulationRepository:
    """Read-only aggregates for the five JNPA what-if scenarios."""

    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    # ------------------------------------------------------------------ engine
    @staticmethod
    def assert_read_only(sql: str) -> None:
        """Refuse anything that is not a pure read. Public so the test suite can
        assert the guard directly rather than only through a query."""
        if not _READ_ONLY.match(sql or ""):
            raise SimulationWriteAttempt(
                f"simulation SQL must start with SELECT or WITH: {sql[:80]!r}")
        if _WRITE_VERB.search(sql or ""):
            raise SimulationWriteAttempt(
                f"simulation SQL contains a write verb: {sql[:80]!r}")

    async def _read(self, purpose: str, sql: str,
                    params: Optional[Mapping[str, Any]] = None,
                    *, api: Optional[str] = None) -> tuple[list[dict], QueryTrace]:
        """Run one read and return its rows plus the trace to publish.

        Fail-soft: a missing table or an unreachable database returns ``[]`` with
        a zero-row trace, so a scenario degrades to "data not available" instead
        of 500-ing the endpoint.

        A failure is RECORDED on the trace, not swallowed. Both an empty table and
        a broken query yield zero rows, and a scenario that cannot tell them apart
        would report "no data in this window" when the truth is "the query did not
        run" — a confidently wrong answer, which is the one thing this layer must
        never produce."""
        self.assert_read_only(sql)
        bound = dict(params or {})
        rows: list[dict] = []
        failure: Optional[str] = None
        try:
            # connect(), never begin() — no transaction, nothing to commit.
            async with get_engine(self._dsn).connect() as conn:
                result = await conn.execute(text(sql), bound)
                rows = [dict(r) for r in result.mappings().all()]
        except Exception as exc:  # noqa: BLE001 — un-migrated / unreachable DB
            # asyncpg surfaces the root cause on __cause__; the SQLAlchemy wrapper
            # text is 20 lines of echoed SQL that would drown the response.
            failure = str(getattr(exc, "orig", None) or exc).splitlines()[0][:300]
            log.warning("simulation.read_failed", purpose=purpose, error=failure)
        return rows, QueryTrace(purpose=purpose, sql=sql, params=bound, api=api,
                                row_count=len(rows), error=failure)

    # ------------------------------------------------------- I-B / II-B: berths
    _BERTH_QUEUE_SQL = """
        SELECT id, terminal, vessel_name, voyage_number, imo_number, berth_number,
               shipping_line, eta, ata, berthing_time, departure_time,
               cargo_operation_start, cargo_operation_end, status
          FROM core.berthing_record
         WHERE (CAST(:terminal AS text) IS NULL OR terminal = CAST(:terminal AS text))
           AND COALESCE(cargo_operation_start, berthing_time, ata, eta)
               BETWEEN :from_ts AND :to_ts
         ORDER BY COALESCE(cargo_operation_start, berthing_time, ata, eta) ASC,
                  berth_number ASC, id ASC
    """

    async def berth_queue(self, *, terminal: Optional[str], from_ts: datetime,
                          to_ts: datetime) -> tuple[list[dict], QueryTrace]:
        """Vessel calls at a terminal whose operation window opens inside
        ``[from_ts, to_ts]``, in the order they occupy the berths.

        Ordered by the best available start timestamp: the operation start when
        reported, else berthing time, else ATA, else ETA — the same COALESCE the
        cascade uses, so ordering and arithmetic can never disagree."""
        return await self._read(
            "berth queue for the cascade window", self._BERTH_QUEUE_SQL,
            {"terminal": terminal, "from_ts": from_ts, "to_ts": to_ts},
            api="POST /api/cargo/simulate/berth-cascade")

    _CALL_MOVES_SQL = """
        SELECT b.id                AS berthing_record_id,
               b.terminal, b.vessel_name, b.voyage_number, b.berth_number,
               b.cargo_operation_start, b.cargo_operation_end,
               b.berthing_time, b.departure_time, b.ata, b.eta,
               m.id                AS moves_id,
               m.vcn, m.discharge_moves, m.load_moves, m.restow_moves,
               m.gross_moves, m.cranes_deployed, m.data_origin, m.source_note
          FROM core.berthing_record b
          LEFT JOIN core.vessel_call_moves m
                 ON m.berthing_record_id = b.id
                 OR (m.terminal = b.terminal
                     AND m.voyage_number = b.voyage_number
                     AND m.vessel_name = b.vessel_name)
         WHERE (CAST(:terminal AS text) IS NULL OR b.terminal = CAST(:terminal AS text))
           AND COALESCE(b.cargo_operation_start, b.berthing_time, b.ata, b.eta)
               BETWEEN :from_ts AND :to_ts
         ORDER BY COALESCE(b.cargo_operation_start, b.berthing_time, b.ata, b.eta) ASC,
                  b.id ASC
    """

    async def calls_with_moves(self, *, terminal: Optional[str], from_ts: datetime,
                               to_ts: datetime) -> tuple[list[dict], QueryTrace]:
        """Vessel calls joined to their move counts (migration 0129).

        The join tries ``berthing_record_id`` first and falls back to the
        (terminal, voyage, vessel) natural key — the two identities 0129 accepts.
        A call the EDI manifest cannot be matched to comes back with NULL moves,
        and the scenario reports it as "productivity not derivable" rather than
        assigning it a number."""
        return await self._read(
            "vessel calls joined to their gross-move counts", self._CALL_MOVES_SQL,
            {"terminal": terminal, "from_ts": from_ts, "to_ts": to_ts},
            api="POST /api/cargo/simulate/crane-productivity")

    # -------------------------------------------------- II-A / III-A: the gate
    _GATE_HOURLY_SQL = """
        SELECT date_trunc('hour', e.truck_in_time)      AS bucket,
               count(*)                                  AS arrivals,
               count(DISTINCT e.truck_no)                AS unique_trucks,
               count(*) FILTER (WHERE e.truck_out_time IS NOT NULL) AS completed,
               round(avg(e.tat_minutes)::numeric, 1)     AS avg_tat_min
          FROM core.eir e
         WHERE e.truck_in_time >= :from_ts
           AND e.truck_in_time <  :to_ts
           AND (CAST(:terminal AS text) IS NULL OR e.terminal ILIKE CAST(:terminal_like AS text))
         GROUP BY date_trunc('hour', e.truck_in_time)
         ORDER BY bucket ASC
    """

    async def gate_hourly_profile(self, *, from_ts: datetime, to_ts: datetime,
                                  terminal: Optional[str] = None
                                  ) -> tuple[list[dict], QueryTrace]:
        """Hourly truck arrivals at the gate over an ARBITRARY window.

        Sourced from ``core.eir.truck_in_time`` — real JNPA gate documents. This
        replaces the only hourly gate view that existed, ``mart.v_gate_throughput``,
        which is pinned to ``WHERE ts > now() - '24:00:00'`` and therefore cannot
        address 1-3 August at all (audit finding, blocker for II-A and III-A)."""
        return await self._read(
            "hourly truck arrivals at the gate (core.eir)", self._GATE_HOURLY_SQL,
            {"from_ts": from_ts, "to_ts": to_ts, "terminal": terminal,
             "terminal_like": f"%{terminal}%" if terminal else None},
            api="GET /api/gate/hourly-profile")

    _GATE_EVENT_HOURLY_SQL = """
        SELECT date_trunc('hour', g.ts)                   AS bucket,
               count(*) FILTER (WHERE g.event_type = 'GATE_ARRIVAL') AS arrivals,
               count(*) FILTER (WHERE g.event_type = 'GATE_IN')      AS gate_in,
               count(*) FILTER (WHERE g.event_type = 'GATE_OUT')     AS gate_out,
               count(DISTINCT g.plate)                    AS unique_trucks
          FROM core.gate_event g
         WHERE g.ts >= :from_ts AND g.ts < :to_ts
           AND (CAST(:gate_id AS text) IS NULL OR g.gate_id = CAST(:gate_id AS text))
         GROUP BY date_trunc('hour', g.ts)
         ORDER BY bucket ASC
    """

    async def gate_event_hourly(self, *, from_ts: datetime, to_ts: datetime,
                                gate_id: Optional[str] = None
                                ) -> tuple[list[dict], QueryTrace]:
        """Hourly gate events — the fallback source when ``core.eir`` is empty for
        the window, and the source of the observed SERVICE rate (GATE_IN
        completions per hour) that the sustained-capacity figure is derived from."""
        return await self._read(
            "hourly gate events (core.gate_event)", self._GATE_EVENT_HOURLY_SQL,
            {"from_ts": from_ts, "to_ts": to_ts, "gate_id": gate_id},
            api="GET /api/gate/hourly-profile")

    _TAS_CAPACITY_SQL = """
        SELECT date_trunc('hour', window_start) AS bucket,
               sum(capacity)                    AS slot_capacity,
               sum(booked)                      AS slot_booked,
               count(*)                         AS windows
          FROM core.tas_appointment
         WHERE window_start >= :from_ts AND window_start < :to_ts
           AND (CAST(:gate_id AS text) IS NULL OR gate_id = CAST(:gate_id AS text))
         GROUP BY date_trunc('hour', window_start)
         ORDER BY bucket ASC
    """

    async def tas_hourly_capacity(self, *, from_ts: datetime, to_ts: datetime,
                                  gate_id: Optional[str] = None
                                  ) -> tuple[list[dict], QueryTrace]:
        """Booked appointment capacity per hour — the DECLARED gate capacity, when
        the slot book has been provisioned for the window. Preferred over a
        derived rate because it is a policy figure rather than an inference."""
        return await self._read(
            "declared hourly gate capacity (core.tas_appointment)",
            self._TAS_CAPACITY_SQL,
            {"from_ts": from_ts, "to_ts": to_ts, "gate_id": gate_id},
            api="GET /api/tas/slots")

    # ------------------------------------------------------ II-A: modal split
    _RAIL_ROAD_DAILY_SQL = """
        SELECT report_date, terminal_code,
               total_teus, imp_teus, exp_teus,
               rakes, rail_dis_teus, rail_ldg_teus, rail_total_teus
          FROM core.perf_daily_traffic
         WHERE period = 'DAY'
           AND report_date >= :from_date AND report_date <= :to_date
           AND (CAST(:terminal AS text) IS NULL OR terminal_code = CAST(:terminal AS text))
         ORDER BY report_date ASC, terminal_code ASC
    """

    async def rail_road_daily(self, *, from_date: date, to_date: date,
                              terminal: Optional[str] = None
                              ) -> tuple[list[dict], QueryTrace]:
        """Daily rail vs total TEUs per terminal — real JNPA daily-report figures
        (``core.perf_daily_traffic``). The authoritative rail volume for II-A."""
        return await self._read(
            "daily rail vs total TEU by terminal (core.perf_daily_traffic)",
            self._RAIL_ROAD_DAILY_SQL,
            {"from_date": from_date, "to_date": to_date, "terminal": terminal},
            api="GET /api/performance/daily/traffic")

    _EVAC_MODE_SQL = """
        SELECT COALESCE(evacuation_mode, 'UNKNOWN') AS evacuation_mode,
               COALESCE(evacuation_mode_source, 'ASSUMED') AS source,
               count(*) AS containers
          FROM core.cargo
         WHERE created_at >= :from_ts AND created_at < :to_ts
         GROUP BY 1, 2
         ORDER BY 1, 2
    """

    async def evacuation_mode_split(self, *, from_ts: datetime, to_ts: datetime
                                    ) -> tuple[list[dict], QueryTrace]:
        """Per-container RAIL / ROAD / UNKNOWN split from ``core.cargo``
        (migration 0128), with the provenance of each label so a DERIVED
        attribution is never presented as a measured one."""
        return await self._read(
            "per-container evacuation mode split (core.cargo, migration 0128)",
            self._EVAC_MODE_SQL, {"from_ts": from_ts, "to_ts": to_ts},
            api="GET /api/cargo?limit=…")

    # -------------------------------------------------- III-B: trips + drivers
    _VEHICLE_TRIPS_SQL = """
        SELECT COALESCE(NULLIF(btrim(e.company), ''), 'UNATTRIBUTED') AS transporter,
               e.truck_no,
               e.truck_in_time::date                    AS trip_date,
               count(*)                                 AS trips,
               count(DISTINCT e.container_number) FILTER (
                   WHERE e.container_number IS NOT NULL) AS containers,
               round(avg(e.tat_minutes)::numeric, 1)     AS avg_tat_min
          FROM core.eir e
         WHERE e.truck_in_time >= :from_ts
           AND e.truck_in_time <  :to_ts
           AND e.truck_no IS NOT NULL
         GROUP BY 1, 2, 3
         ORDER BY 1, 2, 3
    """

    async def vehicle_trips(self, *, from_ts: datetime, to_ts: datetime
                            ) -> tuple[list[dict], QueryTrace]:
        """Trips per vehicle per day, attributed to a transporter.

        ``core.eir.company`` is the transporter printed on the Equipment
        Interchange Report — the only per-trip transporter attribution in the
        database, and real JNPA data. One EIR = one gate trip."""
        return await self._read(
            "trips per vehicle per day by transporter (core.eir)",
            self._VEHICLE_TRIPS_SQL, {"from_ts": from_ts, "to_ts": to_ts},
            api="GET /api/gate-docs/eir?from_date=…&to_date=…")

    _CARGO_FLOW_SQL = """
        SELECT COALESCE(NULLIF(btrim(e.eir_type), ''), 'UNSPECIFIED')  AS flow,
               COALESCE(NULLIF(btrim(e.cfs_to), ''),
                        NULLIF(btrim(e.cfs_from), ''), 'DIRECT')       AS facility,
               count(*)                                                AS trips,
               count(DISTINCT e.truck_no)                              AS vehicles
          FROM core.eir e
         WHERE e.truck_in_time >= :from_ts AND e.truck_in_time < :to_ts
         GROUP BY 1, 2
         ORDER BY trips DESC
    """

    async def cargo_flows(self, *, from_ts: datetime, to_ts: datetime
                          ) -> tuple[list[dict], QueryTrace]:
        """Trip volume by cargo flow (EIR type) and facility — the "which cargo
        flows are most exposed" half of III-B."""
        return await self._read(
            "trip volume by cargo flow and facility (core.eir)",
            self._CARGO_FLOW_SQL, {"from_ts": from_ts, "to_ts": to_ts},
            api="GET /api/gate-docs/eir?from_date=…&to_date=…")

    # ------------------------------------------------------------- yard/pendency
    _PENDENCY_SQL = """
        SELECT COALESCE(evacuation_mode, 'UNKNOWN') AS evacuation_mode,
               lifecycle_status,
               count(*) AS containers
          FROM core.cargo
         WHERE lifecycle_status IN ('VESSEL_DISCHARGED','PENDENCY','YARD_ASSIGNED',
                                    'YARD_POSITION_ALLOCATED','RAKE_ASSIGNED',
                                    'REEFER_PLANNED','SCAN_PENDING','VERIFIED')
           AND is_released = false
         GROUP BY 1, 2
         ORDER BY 1, 2
    """

    async def pendency_snapshot(self) -> tuple[list[dict], QueryTrace]:
        """Containers in the port awaiting evacuation, by mode and lifecycle
        state — the backlog a driver shortage accumulates against."""
        return await self._read(
            "unreleased containers by evacuation mode and lifecycle state",
            self._PENDENCY_SQL, {}, api="GET /api/cargo?status=…")
