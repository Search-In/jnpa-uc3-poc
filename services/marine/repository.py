"""UC-I Marine vessel-call persistence — raw-SQL repository over the shared async engine.

The ONLY layer that speaks SQL to the core.vessel_call* tables. No ORM; parameterised
``text()`` over the cached SQLAlchemy async engine (``jnpa_shared.db.get_engine``),
exactly like :mod:`services.berthing.repository`.

READ-ONLY in this slice: every statement is a SELECT. The write path (PCS / pilot-card
ingestion) lands in a later slice as a separate upload service, mirroring
:class:`services.berthing.BerthingUploadService`.

Touches ONLY the ``core`` schema — nothing in ``jnpa`` is read or written, so the
berthing module and every other UC3 module are unaffected.

Injection-safe, same rules as the berthing repository: filter/sort COLUMN names are
fixed whitelist identifiers defined in this module; every VALUE is a bound parameter.

Two shapes differ from berthing, both traceable to migration 0038:
  * status is FREE-TEXT (deviation D8) — there is no fixed vocabulary to bucket on,
    so :meth:`stats` derives its counters from the typed actual timestamps
    (ata / atd / atc) and returns the status distribution as a dynamic histogram.
  * vessel_call_event permits REPEATED event types at different timestamps
    (deviation D6), so timelines order by event_ts, NOT by a fixed milestone rank
    array the way jnpa.berthing_events does.
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

from .manual_pilot import ManualPilotService

from .state_engine import (EVENT_ARRIVED, EVENT_DEPARTED, in_port_sql, status_order_sql)

log = get_logger("services.marine.repository")


def _data_origin(uploaded_by: Optional[str],
                 data_origin: Optional[str] = None) -> str:
    """Provenance tag for an import: ``API`` (LIVE) or ``MANUAL`` (DEMO).

    Preference order:
      1. Explicit ``data_origin`` from the request (``X-Data-Mode`` → LIVE/DEMO),
         so a SPA upload while the board is on LIVE lands in the LIVE corpus and
         is visible under that toggle.
      2. ``uploaded_by == 'jnpa-api'`` → ``API`` (JNPA sync path, unchanged).
      3. Else ``MANUAL`` (default for anonymous / unscoped uploads).
    """
    if data_origin in ("API", "MANUAL"):
        return data_origin
    return "API" if (uploaded_by or "").strip().lower() == "jnpa-api" else "MANUAL"


# ---------------------------------------------------------------- column whitelists
# core.vessel_call, in migration 0038 declaration order.
_COLUMNS = (
    "call_id", "vcn", "via_no", "imo_no", "vessel_name", "voyage_no", "rotation_no",
    "terminal_id", "berth_id", "purpose", "eta", "etd", "etb", "ata", "atd", "atc",
    "status", "igm_no", "source_note", "created_at", "updated_at",
)
# Human-readable label for the terminal_id FK, so a client does not have to carry the
# reference dimension to render "BMCT" instead of "5". A correlated SCALAR subquery, not a
# JOIN: it cannot change row multiplicity, so every existing query keeps its FROM clause
# and its row count unchanged. NULL when terminal_id is unresolved — additive by design.
_TERMINAL_CODE_EXPR = ("(SELECT t.code FROM core.ref_terminal t "
                       "WHERE t.terminal_id = c.terminal_id) AS terminal_code")
# Same shape for the allotted berth (BERALT), so a client can render 'CB05' without
# carrying core.ref_berth. NULL until a berth is allotted — additive by design.
_BERTH_CODE_EXPR = ("(SELECT b.code FROM core.ref_berth b "
                    "WHERE b.berth_id = c.berth_id) AS berth_code")
_SELECT_COLS = (", ".join(f"c.{col}" for col in _COLUMNS)
                + ", " + _TERMINAL_CODE_EXPR + ", " + _BERTH_CODE_EXPR)

# core.vessel_call_event, in migration 0038 declaration order.
_EVENT_COLUMNS = (
    "event_id", "call_id", "event_type", "event_ts", "berth_id", "source_file",
    "created_at",
)
# Label for the event's berth_id, matching the call projection: a milestone that happened
# at a berth reads 'CB05', not '12'. Scalar subquery, so row multiplicity is unchanged.
_EVENT_BERTH_CODE_EXPR = ("(SELECT b.code FROM core.ref_berth b "
                          "WHERE b.berth_id = e.berth_id) AS berth_code")
_EVENT_SELECT_COLS = (", ".join(f"e.{col}" for col in _EVENT_COLUMNS)
                      + ", " + _EVENT_BERTH_CODE_EXPR)

# Exact-match filters: filter key == column name.
_EQ_FILTERS = ("vcn", "imo_no", "status", "terminal_id", "berth_id")

# Substring (ILIKE) filters: filter key -> column name.
_LIKE_FILTERS = {"vessel": "vessel_name", "voyage": "voyage_no", "via": "via_no",
                 "rotation": "rotation_no"}

_SORTS = {"eta": "c.eta", "etd": "c.etd", "etb": "c.etb", "ata": "c.ata",
          "atd": "c.atd", "atc": "c.atc", "vessel_name": "c.vessel_name",
          "vcn": "c.vcn", "via_no": "c.via_no", "terminal_id": "c.terminal_id",
          "status": "c.status", "updated_at": "c.updated_at", "call_id": "c.call_id"}

# A vessel call has a bounded number of milestones; this caps a pathological read.
_EVENT_PAGE_MAX = 500


class VesselCallRepository:
    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    # ------------------------------------------------------------- filters
    def _where(self, filters: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        conds: list[str] = []
        params: dict[str, Any] = {}
        for col in _EQ_FILTERS:
            val = filters.get(col)
            if val is not None:
                conds.append(f"c.{col} = :{col}")
                params[col] = val
        for key, col in _LIKE_FILTERS.items():
            val = filters.get(key)
            if val:
                conds.append(f"c.{col} ILIKE :{key}")
                params[key] = f"%{str(val).strip()}%"
        # Tri-state: None = no filter, True = VCN assigned, False = still pre-VCN
        # (a CALINF-seeded call that BERMAN has not yet promoted).
        has_vcn = filters.get("has_vcn")
        if has_vcn is True:
            conds.append("c.vcn IS NOT NULL")
        elif has_vcn is False:
            conds.append("c.vcn IS NULL")
        # Arrived but not yet sailed — definition owned by state_engine.in_port_sql().
        if filters.get("in_port"):
            conds.append(in_port_sql("c"))
        if filters.get("eta_from") is not None:
            conds.append("c.eta >= :eta_from")
            params["eta_from"] = filters["eta_from"]
        if filters.get("eta_to") is not None:
            conds.append("c.eta <= :eta_to")
            params["eta_to"] = filters["eta_to"]
        # LIVE / DEMO provenance narrowing. None (no header) => unfiltered, so the
        # SQL is byte-identical to before this feature.
        data_origin = filters.get("data_origin")
        if data_origin is not None:
            conds.append("c.data_origin = :data_origin")
            params["data_origin"] = data_origin
        clause = ("WHERE " + " AND ".join(conds)) if conds else ""
        return clause, params

    @staticmethod
    def _origin_and(data_origin: Optional[str], alias: str) -> tuple[str, dict]:
        """`AND <alias>.data_origin = :data_origin` when a provenance is pinned, else
        an empty fragment — so the single-row lookups stay byte-identical when None."""
        if data_origin is None:
            return "", {}
        return f" AND {alias}.data_origin = :data_origin", {"data_origin": data_origin}

    # ------------------------------------------------------------- list + count
    async def list_calls(self, filters: Mapping[str, Any], *, sort: str,
                         direction: str, limit: int, offset: int) -> list[dict]:
        clause, params = self._where(filters)
        order_col = _SORTS.get(sort, "c.updated_at")
        order_dir = "ASC" if str(direction).lower() == "asc" else "DESC"
        params.update({"limit": limit, "offset": offset})
        sql = (f"SELECT {_SELECT_COLS} FROM core.vessel_call c {clause} "
               f"ORDER BY {order_col} {order_dir} NULLS LAST, c.call_id DESC "
               "LIMIT :limit OFFSET :offset")
        async with get_engine(self._dsn).connect() as conn:
            result = await conn.execute(text(sql), params)
            return [dict(r) for r in result.mappings().all()]

    async def count(self, filters: Mapping[str, Any]) -> int:
        clause, params = self._where(filters)
        async with get_engine(self._dsn).connect() as conn:
            return int((await conn.execute(
                text(f"SELECT count(*) FROM core.vessel_call c {clause}"),
                params)).scalar() or 0)

    # ------------------------------------------------------------- single-call lookups
    async def get(self, call_id: int, *,
                  data_origin: Optional[str] = None) -> Optional[dict]:
        frag, extra = self._origin_and(data_origin, "c")
        async with get_engine(self._dsn).connect() as conn:
            row = (await conn.execute(text(
                f"SELECT {_SELECT_COLS} FROM core.vessel_call c "
                f"WHERE c.call_id = :call_id{frag}"),
                {"call_id": call_id, **extra})).mappings().first()
        return dict(row) if row else None

    async def get_by_vcn(self, vcn: str, *,
                         data_origin: Optional[str] = None) -> Optional[dict]:
        """Resolve the full PCS VCN (e.g. INNSA1BM0R3119) to at most one call.

        Single-valued by construction: uq_vessel_call_vcn (migration 0038 [D4]).
        """
        frag, extra = self._origin_and(data_origin, "c")
        async with get_engine(self._dsn).connect() as conn:
            row = (await conn.execute(text(
                f"SELECT {_SELECT_COLS} FROM core.vessel_call c WHERE c.vcn = :vcn{frag}"),
                {"vcn": vcn, **extra})).mappings().first()
        return dict(row) if row else None

    async def get_by_via(self, via_no: str, *,
                         data_origin: Optional[str] = None) -> list[dict]:
        """Resolve a short VIA number (e.g. S0561) — MAY match several calls.

        Unlike the VCN, short VIA numbers recycle across years (migration 0038 [D10]),
        so this deliberately returns a list ordered newest-first rather than one row.
        """
        frag, extra = self._origin_and(data_origin, "c")
        async with get_engine(self._dsn).connect() as conn:
            rows = (await conn.execute(text(
                f"SELECT {_SELECT_COLS} FROM core.vessel_call c WHERE c.via_no = :via_no{frag} "
                "ORDER BY c.eta DESC NULLS LAST, c.call_id DESC"),
                {"via_no": via_no, **extra})).mappings().all()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------- events
    async def list_events(self, call_id: int, *, limit: int = _EVENT_PAGE_MAX,
                          offset: int = 0,
                          data_origin: Optional[str] = None) -> list[dict]:
        """Actuals for one call, chronological.

        Ordered by event_ts (not by a milestone rank array as jnpa.berthing_events is):
        migration 0038 [D6] permits repeated event types at different timestamps, so
        the timestamp is the only sound ordering key.
        """
        frag, extra = self._origin_and(data_origin, "e")
        async with get_engine(self._dsn).connect() as conn:
            rows = (await conn.execute(text(
                f"SELECT {_EVENT_SELECT_COLS} FROM core.vessel_call_event e "
                f"WHERE e.call_id = :call_id{frag} "
                "ORDER BY e.event_ts, e.event_id "
                "LIMIT :limit OFFSET :offset"),
                {"call_id": call_id, "limit": min(int(limit), _EVENT_PAGE_MAX),
                 "offset": offset, **extra})).mappings().all()
        return [dict(r) for r in rows]

    async def timeline(self, call_id: int, *,
                       data_origin: Optional[str] = None) -> Optional[dict]:
        """One call plus its ordered actuals. None when the call does not exist."""
        call = await self.get(call_id, data_origin=data_origin)
        if call is None:
            return None
        call["events"] = await self.list_events(call_id, data_origin=data_origin)
        return call

    # ------------------------------------------------------------- stats
    async def stats(self, filters: Mapping[str, Any]) -> dict:
        """UC-I aggregates.

        Counters are derived from the typed actual timestamps rather than from status
        buckets: migration 0038 [D8] leaves status free-text, so there is no fixed
        vocabulary to FILTER on. The observed status values are returned separately as
        a dynamic histogram.

        avg_pre_berth_delay_hours may be NEGATIVE (a vessel arriving ahead of its ETA);
        that is meaningful and is deliberately not filtered out.
        """
        clause, params = self._where(filters)
        sql = (
            "SELECT count(*) AS total, "
            "  count(*) FILTER (WHERE c.vcn IS NOT NULL) AS with_vcn, "
            "  count(*) FILTER (WHERE c.vcn IS NULL)     AS without_vcn, "
            "  count(*) FILTER (WHERE c.ata IS NOT NULL) AS arrived, "
            f"  count(*) FILTER (WHERE {in_port_sql('c')}) AS in_port, "
            "  count(*) FILTER (WHERE c.atc IS NOT NULL) AS ops_completed, "
            "  count(*) FILTER (WHERE c.atd IS NOT NULL) AS departed, "
            "  count(DISTINCT c.terminal_id) AS terminals, "
            "  round((avg(extract(epoch FROM (c.atd - c.ata)) / 3600.0) "
            "         FILTER (WHERE c.ata IS NOT NULL AND c.atd IS NOT NULL "
            "                 AND c.atd >= c.ata))::numeric, 1) AS avg_turnaround_hours, "
            "  round((avg(extract(epoch FROM (c.ata - c.eta)) / 3600.0) "
            "         FILTER (WHERE c.eta IS NOT NULL "
            "                 AND c.ata IS NOT NULL))::numeric, 1) AS avg_pre_berth_delay_hours "
            f"FROM core.vessel_call c {clause}"
        )
        async with get_engine(self._dsn).connect() as conn:
            row = (await conn.execute(text(sql), params)).mappings().first()
            by_status = (await conn.execute(text(
                "SELECT c.status, count(*) AS n "
                f"FROM core.vessel_call c {clause} "
                "GROUP BY c.status ORDER BY n DESC, c.status NULLS LAST"),
                params)).mappings().all()
            by_terminal = (await conn.execute(text(
                "SELECT c.terminal_id, count(*) AS n, "
                f"  count(*) FILTER (WHERE {in_port_sql('c')}) AS in_port "
                f"FROM core.vessel_call c {clause} "
                "GROUP BY c.terminal_id ORDER BY c.terminal_id NULLS LAST"),
                params)).mappings().all()
        r = dict(row) if row else {}
        return {
            "total": int(r.get("total") or 0),
            "with_vcn": int(r.get("with_vcn") or 0),
            "without_vcn": int(r.get("without_vcn") or 0),
            "arrived": int(r.get("arrived") or 0),
            "in_port": int(r.get("in_port") or 0),
            "ops_completed": int(r.get("ops_completed") or 0),
            "departed": int(r.get("departed") or 0),
            "terminals": int(r.get("terminals") or 0),
            "avg_turnaround_hours": (
                float(r["avg_turnaround_hours"])
                if r.get("avg_turnaround_hours") is not None else None),
            "avg_pre_berth_delay_hours": (
                float(r["avg_pre_berth_delay_hours"])
                if r.get("avg_pre_berth_delay_hours") is not None else None),
            "by_status": [{"status": s["status"], "count": int(s["n"])} for s in by_status],
            # terminal_id stays numeric until core.ref_terminal exists to label it.
            "by_terminal": [{"terminal_id": t["terminal_id"], "count": int(t["n"]),
                             "in_port": int(t["in_port"])} for t in by_terminal],
        }

    # ================================================================ Data Upload / import
    # Write path for the Marine CSV Data-Upload sub-module. Mirrors
    # services.berthing.repository.BerthingRepository: sha256 file dedup on
    # core.marine_import_files + a VCN upsert on core.vessel_call (never overwrites with
    # NULL — COALESCE enrichment). Read methods above are unchanged.
    async def find_file_by_hash(self, file_hash: str,
                                data_origin: Optional[str] = None) -> Optional[dict]:
        # Per-origin dedup: the SAME bytes delivered by both the API and a manual dump
        # are kept once PER ORIGIN (ledger UNIQUE is (file_hash, data_origin)). A None
        # data_origin leaves the lookup unfiltered, byte-identical to before.
        frag, extra = ("", {})
        if data_origin is not None:
            frag = " AND data_origin = :data_origin"
            extra = {"data_origin": data_origin}
        async with get_engine(self._dsn).connect() as conn:
            row = (await conn.execute(text(
                "SELECT id, filename, status, total_rows, success_rows, failed_rows, "
                "duplicate_rows, created_at "
                "FROM core.marine_import_files WHERE file_hash = :h" + frag),
                {"h": file_hash, **extra})).mappings().first()
        return dict(row) if row else None

    # ------------------------------------------------------------- param builders
    @staticmethod
    def _vessel_params(rec: Mapping[str, Any]) -> dict:
        return {k: rec.get(k) for k in _VESSEL_COLS}

    @staticmethod
    def _call_params(rec: Mapping[str, Any], data_origin: str) -> dict:
        return {**{k: rec.get(k) for k in _CALL_COLS}, "data_origin": data_origin}

    @staticmethod
    def _sea_channel_params(rec: Mapping[str, Any], fid: int, data_origin: str) -> dict:
        return {"name": rec.get("name"), "section_label": rec.get("section_label"),
                "area_ha": rec.get("area_ha"), "length_m": rec.get("length_m"),
                "geom_geojson": json.dumps(rec.get("geom_geojson") or {}, default=str),
                "row_sha256": rec.get("row_sha256"), "import_file_id": fid,
                "data_origin": data_origin}

    @staticmethod
    def _bathymetry_sounding_params(rec: Mapping[str, Any], survey_id: int, fid: int,
                                    data_origin: str) -> dict:
        """Canonical sounding record -> bind params.

        Positional fields are passed through as-is (already coerced to float-or-None by the
        canonical model), so an ungeoreferenced chart writes NULL easting/northing/lat/lon
        and keeps only its page coordinates — that is valid data, not a defect.
        """
        p = {k: rec.get(k) for k in (
            "easting_m", "northing_m", "lat", "lon", "depth_m",
            "page_x_pt", "page_y_pt")}
        p["above_design"] = bool(rec.get("above_design"))
        p["row_sha256"] = rec.get("row_sha256")
        p["survey_id"] = survey_id
        p["import_file_id"] = fid
        p["data_origin"] = data_origin
        return p

    @staticmethod
    def _port_craft_params(rec: Mapping[str, Any], fid: int, data_origin: str) -> dict:
        p = {k: rec.get(k) for k in (
            "name", "craft_type", "owned_or_hired", "owner_name", "year_built",
            "loa_m", "breadth_m", "draft_m", "main_engines", "bollard_pull_t",
            "design_speed_kn")}
        p["extras"] = json.dumps(rec.get("extras") or {}, default=str)
        p["import_file_id"] = fid
        p["data_origin"] = data_origin
        return p

    @staticmethod
    def _pilotage_params(rec: Mapping[str, Any], fid: int, data_origin: str) -> dict:
        p = {k: rec.get(k) for k in (
            "movement_type", "via_no", "imo_no", "vessel_name", "pilot_code",
            "vessel_condition", "draft_fwd_m", "draft_aft_m", "pilot_boarded_at",
            "first_line_at", "all_fast_at", "pilot_disembarked_at", "berth_vacated_at",
            "anchor_down_at", "anchor_up_at", "submitted_at", "row_sha256",
            "from_berth_code", "to_berth_code")}
        p["extras"] = json.dumps(rec.get("extras") or {}, default=str)
        p["import_file_id"] = fid
        p["data_origin"] = data_origin
        return p

    @staticmethod
    def _err_row(fid: int, e: Mapping[str, Any]) -> dict:
        return {"fid": fid, "rn": e.get("row_number"),
                "msg": (f"{e.get('column_name') or ''}: "
                        f"{e.get('error_detail') or e.get('error_code') or ''}").strip(": ")[:2000],
                "raw": (None if e.get("raw_value") is None else str(e.get("raw_value"))[:2000])}

    async def _resolve_call_id(self, conn, e: Mapping[str, Any]) -> Optional[int]:
        """Resolve a vessel_call_event to a call_id, priority VCN → (imo,voyage) → VIA.
        Returns None when unresolved (the caller records an 'unresolved_call' error and
        NEVER inserts a stub call). Runs on the SAME connection/txn as the upserts, so an
        event resolves a call created earlier in this very file."""
        vcn = e.get("vcn")
        if vcn:
            row = (await conn.execute(text(_RESOLVE_BY_VCN), {"vcn": vcn})).first()
            if row:
                return int(row[0])
        imo, voyage = e.get("imo_no"), e.get("via_no")
        if imo and voyage:
            row = (await conn.execute(text(_RESOLVE_BY_IMO_VOYAGE),
                                      {"imo_no": imo, "voyage_no": voyage})).first()
            if row:
                return int(row[0])
        via = e.get("via_no")
        if via:
            row = (await conn.execute(text(_RESOLVE_BY_VIA), {"via_no": via})).first()
            if row:
                return int(row[0])
        return None

    async def persist(self, records: Sequence[Mapping[str, Any]], *, filename: str,
                      file_hash: str, physical_format: str,
                      document_type: Optional[str] = None,
                      parse_errors: Optional[Sequence[Mapping[str, Any]]] = None,
                      parse_invalid: int = 0, parse_duplicate: int = 0,
                      file_size: Optional[int] = None, uploaded_by: Optional[str] = None,
                      source: str = "UPLOAD", override: bool = False,
                      data_origin: Optional[str] = None) -> dict:
        """Persist normalized parser records across core.vessel / core.vessel_call /
        core.vessel_call_event in ONE transaction (multi-target router).

        Processing order (required for BERMAN promotion, per decision 1):
            vessel → CALINF pre-VCN vessel_call → BERMAN VCN vessel_call → events.
        BERMAN promotes an existing pre-VCN seed on (imo_no, voyage_no) before its VCN
        upsert, so CALINF+BERMAN enrich ONE call rather than making two rows. Events
        resolve their call_id (VCN → imo+voyage → VIA); an unresolved event becomes a
        row error, never a stub call (decision 2). File-level sha256 dedup unchanged.

        ``override`` re-processes a file already in the ledger. It is NOT a delete: every
        write below is already an UPSERT (vessel on imo_no, vessel_call on vcn / the
        pre-VCN key, events ON CONFLICT DO NOTHING), so replaying the same records
        refreshes the business rows in place and re-runs _PROJECT_CALL_ACTUALS. Nothing is
        truncated and no historical row is removed. Default False, so the normal import
        path — including SKIPPED_DUPLICATE — is byte-for-byte unchanged.

        Returns the refined counter dict; the existing response keys (status, inserted,
        updated, duplicate_file, success_rows) are preserved and callers read those."""
        # Provenance for this import (LIVE=API vs DEMO=MANUAL). Dedup is PER-ORIGIN, so
        # the same bytes from the API and from a manual dump each land once — and an
        # override reopens THIS origin's ledger row, never the other corpus's.
        # ``data_origin`` (from X-Data-Mode) wins when the SPA pins LIVE/DEMO.
        origin = _data_origin(uploaded_by, data_origin)
        existing = await self.find_file_by_hash(file_hash, origin)
        if existing is not None and not override:
            return {"file_id": existing["id"], "status": "SKIPPED_DUPLICATE",
                    "inserted": 0, "updated": 0, "duplicate": 0, "failed": 0, "invalid": 0,
                    "success_rows": existing["success_rows"], "duplicate_file": True}

        parse_errors = list(parse_errors or [])
        envelope = {"filename": filename, "file_hash": file_hash,
                    "physical_format": physical_format, "document_type": document_type,
                    "uploaded_by": uploaded_by, "total_rows": len(records), "source": source,
                    "data_origin": origin}

        def _target(r: Mapping[str, Any]) -> str:
            """The record's routing target, or "" when absent.

            Deliberately NOT defaulted to "vessel_call": that fallback silently filed an
            untagged record into the vessel-call spine, and because the unknown-target
            check runs on this value, a MISSING tag could never be reported. An empty
            string matches no partition, so it falls into `unknown` and becomes a typed
            validation error — a parser that forgets to tag its records now fails loudly."""
            return r.get("_target") or ""

        vessels = [r for r in records if _target(r) == "vessel"]
        calls_pre = [r for r in records if _target(r) == "vessel_call" and not r.get("vcn")]
        calls_vcn = [r for r in records if _target(r) == "vessel_call" and r.get("vcn")]
        events = [r for r in records if _target(r) == "vessel_call_event"]
        pilots = [r for r in records if _target(r) == "pilot"]
        pilotages = [r for r in records if _target(r) == "pilotage"]
        port_crafts = [r for r in records if _target(r) == "port_craft"]
        sea_channels = [r for r in records if _target(r) == "sea_channel"]
        bathy_soundings = [r for r in records if _target(r) == "bathymetry_sounding"]
        _KNOWN = ("vessel", "vessel_call", "vessel_call_event", "pilot", "pilotage",
                  "port_craft", "sea_channel", "bathymetry_sounding")
        unknown = [r for r in records if _target(r) not in _KNOWN]

        ins = upd = dup = 0
        repo_errors: list[dict] = []
        try:
            async with get_engine(self._dsn).begin() as conn:
                if existing is not None:
                    # Override: reopen THIS file's ledger row (file_hash is unique, so a
                    # second row is impossible) and clear the previous run's row errors,
                    # so the entry reports the run it just performed.
                    fid = int(existing["id"])
                    await conn.execute(text(_FILE_REOPEN), {**envelope, "id": fid})
                    await conn.execute(text(_CLEAR_FILE_ERRORS), {"id": fid})
                else:
                    fid = (await conn.execute(text(_FILE_INSERT),
                                              envelope)).mappings().first()["id"]

                # 1. VESPRO → core.vessel (+ insurance)
                for v in vessels:
                    r = (await conn.execute(text(_VESSEL_UPSERT), self._vessel_params(v))).mappings().first()
                    if r is None:
                        continue
                    ins += bool(r["inserted"]); upd += (not bool(r["inserted"]))
                    for pi in (v.get("_insurance") or []):
                        if pi.get("pi_club"):
                            await conn.execute(text(_VESSEL_INSURANCE_UPSERT),
                                               {"imo_no": v.get("imo_no"), "pi_club": pi.get("pi_club"),
                                                "valid_until": pi.get("valid_until")})

                # 2. CALINF pre-VCN seed (dedup on (imo_no, voyage_no) WHERE vcn IS NULL)
                for c in calls_pre:
                    r = (await conn.execute(text(_VESSEL_CALL_PREVCN_UPSERT), self._call_params(c, origin))).mappings().first()
                    if r is None:
                        continue
                    ins += bool(r["inserted"]); upd += (not bool(r["inserted"]))

                # 3. BERMAN: promote the seed (set VCN on the pre-VCN row) then VCN upsert
                for c in calls_vcn:
                    await conn.execute(text(_VESSEL_CALL_PROMOTE),
                                       {"vcn": c.get("vcn"), "imo_no": c.get("imo_no"),
                                        "voyage_no": c.get("voyage_no")})
                    r = (await conn.execute(text(_VESSEL_CALL_UPSERT), self._call_params(c, origin))).mappings().first()
                    if r is None:
                        continue
                    ins += bool(r["inserted"]); upd += (not bool(r["inserted"]))

                # 4. VESARR/VESDEP → events (resolve call_id; unresolved → error, no stub)
                # Calls touched here get their actuals projected in step 4b.
                touched_calls: set[int] = set()
                for e in events:
                    cid = await self._resolve_call_id(conn, e)
                    if cid is None:
                        repo_errors.append({
                            "row_number": None, "column_name": "call", "error_code": "unresolved_call",
                            "error_detail": (f"no vessel_call for {e.get('event_type')} "
                                             f"(VCN={e.get('vcn')}, voyage={e.get('via_no')})"),
                            "raw_value": e.get("vcn") or e.get("via_no")})
                        continue
                    r = (await conn.execute(text(_EVENT_INSERT),
                                            {"call_id": cid, "event_type": e.get("event_type"),
                                             "event_ts": e.get("event_ts"),
                                             "berth_code": e.get("berth_code"),
                                             "data_origin": origin})).mappings().first()
                    if r is not None:
                        ins += 1
                    else:
                        dup += 1  # ON CONFLICT DO NOTHING — same actual already stored
                    # Project even on a duplicate: the milestone may be unchanged while the
                    # call's ata/atd were never back-filled by an earlier build.
                    touched_calls.add(cid)

                # 4b. Roll the ledger's actuals up onto each touched call (ata / atd only).
                # Deliberately NOT counted in ins/upd: this is a derived projection of rows
                # already counted in step 4, and double-counting would misreport the import.
                for cid in sorted(touched_calls):
                    await conn.execute(text(_PROJECT_CALL_ACTUALS), {"call_id": cid})

                # 5. PILOT roster (upsert before pilotage so the FK resolves)
                for p in pilots:
                    r = (await conn.execute(text(_PILOT_UPSERT),
                                            {"pilot_code": p.get("pilot_code"),
                                             "name": p.get("name")})).mappings().first()
                    if r is not None:
                        ins += bool(r["inserted"]); upd += (not bool(r["inserted"]))

                # 6. PILOTAGE movements (resolve-or-NULL berth/call; row-hash idempotent)
                for pg in pilotages:
                    r = (await conn.execute(text(_PILOTAGE_INSERT),
                                            self._pilotage_params(pg, fid, origin))).mappings().first()
                    if r is not None:
                        ins += 1
                    else:
                        dup += 1  # ON CONFLICT (row_sha256) DO NOTHING — same row already stored

                # 7. PORT CRAFT register (upsert on the natural key `name`)
                for pc in port_crafts:
                    r = (await conn.execute(text(_PORT_CRAFT_UPSERT),
                                            self._port_craft_params(pc, fid, origin))).mappings().first()
                    if r is not None:
                        ins += bool(r["inserted"]); upd += (not bool(r["inserted"]))

                # 8. SEA CHANNEL geometry (row-hash idempotent; name is not unique)
                for sc in sea_channels:
                    r = (await conn.execute(text(_SEA_CHANNEL_INSERT),
                                            self._sea_channel_params(sc, fid, origin))).mappings().first()
                    if r is not None:
                        ins += 1
                    else:
                        dup += 1  # ON CONFLICT (row_sha256) DO NOTHING

                # 9. BATHYMETRY soundings (resolve survey_id from drawing_no; auto-ensure
                #    the survey header on first sight so chart PDF/JSON import works
                #    without a separate register step). Row-hash idempotent, inserted in
                #    batches of _BATHYMETRY_BATCH: one chart is 15k-30k soundings, so a
                #    per-row execute would make an import take hours.
                survey_ids: dict[str, Optional[int]] = {}
                pending: list[dict] = []
                for bs in bathy_soundings:
                    dn = bs.get("drawing_no")
                    if not dn:
                        repo_errors.append({
                            "row_number": None, "column_name": "drawing_no",
                            "error_code": "missing_drawing_no",
                            "error_detail": "bathymetry sounding has no drawing_no",
                            "raw_value": None})
                        continue
                    if dn not in survey_ids:
                        sid = (await conn.execute(
                            text(_RESOLVE_SURVEY_BY_DRAWING), {"drawing_no": dn})).scalar()
                        if sid is None:
                            sid = (await conn.execute(text(_ENSURE_SURVEY), {
                                "drawing_no": dn,
                                "file_path": filename,
                                "data_origin": origin,
                            })).scalar()
                        survey_ids[dn] = sid
                    sid = survey_ids[dn]
                    if sid is None:
                        repo_errors.append({
                            "row_number": None, "column_name": "drawing_no",
                            "error_code": "unresolved_survey",
                            "error_detail": (f"could not resolve or create "
                                             f"core.bathymetry_survey for drawing_no {dn!r}"),
                            "raw_value": dn})
                        continue
                    pending.append(self._bathymetry_sounding_params(bs, sid, fid, origin))

                # Hashes already written by an EARLIER batch of this same transaction are
                # invisible to the pre-filter SELECT, so track them here too — otherwise a
                # payload repeating a sounding across batches would be counted twice.
                seen_hashes: set[str] = set()
                for start in range(0, len(pending), _BATHYMETRY_BATCH):
                    chunk = pending[start:start + _BATHYMETRY_BATCH]
                    hashes = [p["row_sha256"] for p in chunk]
                    existing = set((await conn.execute(
                        text(_BATHYMETRY_EXISTING_HASHES), {"hashes": hashes})).scalars().all())
                    fresh: list[dict] = []
                    for p in chunk:
                        h = p["row_sha256"]
                        if h in existing or h in seen_hashes:
                            dup += 1
                            continue
                        seen_hashes.add(h)
                        fresh.append(p)
                    if fresh:
                        # One round trip for the whole chunk.
                        await conn.execute(text(_BATHYMETRY_SOUNDING_INSERT), fresh)
                        ins += len(fresh)

                for u in unknown:
                    raw = u.get("_target")
                    detail = (f"unknown record target: {raw}" if raw
                              else "record has no _target: cannot route to a table")
                    repo_errors.append({
                        "row_number": None, "column_name": "_target", "error_code": "unknown_target",
                        "error_detail": detail, "raw_value": raw})

                all_errors = parse_errors + repo_errors
                if all_errors:
                    await conn.execute(text(_ERROR_INSERT), [self._err_row(fid, e) for e in all_errors])

                failed = parse_invalid + len(repo_errors)
                total_dup = parse_duplicate + dup
                success = ins + upd
                status = "SUCCESS" if failed == 0 else ("PARTIAL" if success > 0 else "FAILED")
                await conn.execute(text(
                    "UPDATE core.marine_import_files SET status=:st, success_rows=:s, "
                    "failed_rows=:f, duplicate_rows=:d, updated_at=now() WHERE id=:id"),
                    {"st": status, "s": success, "f": failed, "d": total_dup, "id": fid})

            # IMPORTED DATA ALWAYS WINS. If this import produced pilotage for a call an
            # operator had manually assigned, that manual row is deactivated now — it is
            # retained (never deleted) so the operator action stays auditable.
            #
            # Runs AFTER the import transaction has committed and is deliberately not
            # allowed to fail the import: the manual record is subordinate data, and a
            # problem deactivating it must never roll back real imported pilotage. The
            # projection's reader independently shadows manual rows behind imported ones,
            # so a failure here cannot surface stale manual state either.
            try:
                await ManualPilotService(self._dsn).supersede_imported()
            except Exception as exc:  # noqa: BLE001 — subordinate to the import, never fatal
                log.warning("marine.manual_supersede_failed", extra={"error": str(exc)})

            return {"file_id": fid, "status": status, "inserted": ins, "updated": upd,
                    "duplicate": total_dup, "failed": failed, "invalid": parse_invalid,
                    "success_rows": success, "duplicate_file": False}
        except IntegrityError:
            dupf = await self.find_file_by_hash(file_hash, origin)
            if dupf is not None:
                return {"file_id": dupf["id"], "status": "SKIPPED_DUPLICATE", "inserted": 0,
                        "updated": 0, "duplicate": 0, "failed": 0, "invalid": 0,
                        "success_rows": dupf["success_rows"], "duplicate_file": True}
            return await self._record_failure(envelope, "integrity_error")
        except Exception as exc:  # noqa: BLE001 — record + surface as FAILED, never partial
            log.warning("marine.persist_failed", extra={"filename": filename, "error": str(exc)})
            return await self._record_failure(envelope, str(exc))

    async def _record_failure(self, envelope: Mapping[str, Any], detail: str) -> dict:
        row = dict(envelope); row["error_detail"] = detail[:4000]
        try:
            async with get_engine(self._dsn).begin() as conn:
                fid = (await conn.execute(text(_FILE_INSERT_FAILED), row)).mappings().first()["id"]
                await conn.execute(text(
                    "INSERT INTO core.marine_import_errors (import_file_id, row_number, "
                    "error_message, raw_data) VALUES (:fid, NULL, :d, NULL)"),
                    {"fid": fid, "d": detail[:4000]})
            fail_id: Optional[int] = fid
        except Exception as exc:  # noqa: BLE001
            log.error("marine.failure_record_failed", extra={"error": str(exc)})
            fail_id = None
        return {"file_id": fail_id, "status": "FAILED", "inserted": 0, "updated": 0,
                "success_rows": 0, "duplicate_file": False}

    async def record_rejected_upload(self, *, physical_format: str, filename: str,
                                     file_hash: str, uploaded_by: Optional[str],
                                     detail: str, errors: Sequence[Mapping[str, Any]],
                                     document_type: Optional[str] = None,
                                     data_origin: Optional[str] = None) -> Optional[int]:
        """Record a structurally-rejected upload (bad template / unreadable / no valid
        rows) as a FAILED ledger row so it appears in history, with its errors. Writes
        NO vessel_call rows. De-dupes on file_hash (per-origin)."""
        origin = _data_origin(uploaded_by, data_origin)
        existing = await self.find_file_by_hash(file_hash, origin)
        if existing is not None:
            return existing["id"]
        envelope = {"filename": filename, "file_hash": file_hash,
                    "physical_format": physical_format, "document_type": document_type,
                    "uploaded_by": uploaded_by, "total_rows": 0, "source": "UPLOAD",
                    "error_detail": detail[:4000], "data_origin": origin}
        try:
            async with get_engine(self._dsn).begin() as conn:
                fid = (await conn.execute(text(_FILE_INSERT_FAILED), envelope)).mappings().first()["id"]
            await self.add_row_errors(fid, errors)
            return fid
        except Exception as exc:  # noqa: BLE001
            log.warning("marine.reject_record_failed", extra={"error": str(exc)})
            return None

    async def add_row_errors(self, file_id: int, errors: Sequence[Mapping[str, Any]]) -> None:
        rows = [{"fid": file_id, "rn": e.get("row_number"),
                 "msg": (f"{e.get('column_name') or ''}: "
                         f"{e.get('error_detail') or e.get('error_code') or ''}").strip(": ")[:2000],
                 "raw": (None if e.get("raw_value") is None else str(e.get("raw_value"))[:2000])}
                for e in errors]
        if not rows:
            return
        async with get_engine(self._dsn).begin() as conn:
            await conn.execute(text(
                "INSERT INTO core.marine_import_errors (import_file_id, row_number, "
                "error_message, raw_data) VALUES (:fid, :rn, :msg, :raw)"), rows)

    async def mark_partial(self, file_id: int, *, failed_rows: int, duplicate_rows: int = 0) -> None:
        async with get_engine(self._dsn).begin() as conn:
            await conn.execute(text(
                "UPDATE core.marine_import_files SET status='PARTIAL', failed_rows=:f, "
                "duplicate_rows=:d, updated_at=now() WHERE id=:id"),
                {"f": failed_rows, "d": duplicate_rows, "id": file_id})

    async def set_duplicates(self, file_id: int, *, duplicate_rows: int) -> None:
        async with get_engine(self._dsn).begin() as conn:
            await conn.execute(text(
                "UPDATE core.marine_import_files SET duplicate_rows=:d, updated_at=now() "
                "WHERE id=:id"), {"d": duplicate_rows, "id": file_id})

    # ------------------------------------------------------------- ledger reads
    @staticmethod
    def _file_where(filters: Mapping[str, Any]) -> tuple[str, dict]:
        clauses, params = [], {}
        for col in ("status", "source", "data_origin"):
            if filters.get(col) is not None:
                clauses.append(f"{col} = :{col}")
                params[col] = filters[col]
        return ((" WHERE " + " AND ".join(clauses)) if clauses else ""), params

    async def list_files(self, *, filters: Mapping[str, Any], limit: int, offset: int) -> list[dict]:
        where, params = self._file_where(filters)
        params.update(limit=limit, offset=offset)
        async with get_engine(self._dsn).connect() as conn:
            res = await conn.execute(text(
                "SELECT id, filename, file_hash, physical_format, document_type, uploaded_by, status, "
                "total_rows, success_rows, failed_rows, duplicate_rows, source, "
                "error_detail, created_at, updated_at "
                f"FROM core.marine_import_files{where} "
                "ORDER BY id DESC LIMIT :limit OFFSET :offset"), params)
            return [dict(r) for r in res.mappings().all()]

    async def count_files(self, *, filters: Mapping[str, Any]) -> int:
        where, params = self._file_where(filters)
        async with get_engine(self._dsn).connect() as conn:
            return int((await conn.execute(
                text(f"SELECT count(*) FROM core.marine_import_files{where}"), params)).scalar() or 0)

    async def get_file(self, file_id: int) -> Optional[dict]:
        async with get_engine(self._dsn).connect() as conn:
            row = (await conn.execute(text(
                "SELECT id, filename, file_hash, physical_format, document_type, uploaded_by, status, "
                "total_rows, success_rows, failed_rows, duplicate_rows, source, "
                "error_detail, created_at, updated_at "
                "FROM core.marine_import_files WHERE id = :id"), {"id": file_id})).mappings().first()
        return dict(row) if row else None

    async def list_file_errors(self, file_id: int, *, limit: int, offset: int) -> list[dict]:
        async with get_engine(self._dsn).connect() as conn:
            res = await conn.execute(text(
                "SELECT id, row_number, error_message, raw_data, created_at "
                "FROM core.marine_import_errors WHERE import_file_id = :id "
                "ORDER BY id LIMIT :limit OFFSET :offset"),
                {"id": file_id, "limit": limit, "offset": offset})
            return [dict(r) for r in res.mappings().all()]


# --------------------------------------------------------------------------- SQL
_FILE_INSERT = """
INSERT INTO core.marine_import_files
    (filename, file_hash, physical_format, document_type, uploaded_by, total_rows, status,
     source, data_origin)
VALUES
    (:filename, :file_hash, :physical_format, :document_type, :uploaded_by, :total_rows,
     'PENDING', :source, :data_origin)
RETURNING id
"""

# A FAILED ledger row used to block re-upload forever, because its hash was taken — so the
# failure insert UPSERTS on the hash instead.
#
# THE TARGET IS (file_hash, data_origin), INFERRED, NOT THE CONSTRAINT NAME. Migration 0120
# drops `uq_marine_import_file_hash` and replaces it with the per-origin unique INDEX
# uq_marine_import_file_hash_origin (file_hash, data_origin) — an index has no constraint
# name to bind to, so `ON CONFLICT ON CONSTRAINT uq_marine_import_file_hash` now raises
# "constraint does not exist". Inference over the same column pair matches it, and keeps the
# retry scoped to ONE origin: a manual re-upload cannot overwrite the API corpus's row for
# the same bytes.
_FILE_INSERT_FAILED = """
INSERT INTO core.marine_import_files
    (filename, file_hash, physical_format, document_type, uploaded_by, total_rows, status,
     error_detail, source, data_origin)
VALUES
    (:filename, :file_hash, :physical_format, :document_type, :uploaded_by, :total_rows,
     'FAILED', :error_detail, :source, :data_origin)
ON CONFLICT (file_hash, data_origin) DO UPDATE SET
    status       = 'FAILED',
    error_detail = EXCLUDED.error_detail,
    total_rows   = EXCLUDED.total_rows,
    updated_at   = now()
RETURNING id
"""

# OVERRIDE re-import: reopen the EXISTING ledger row for this file instead of inserting a
# second one. (file_hash, data_origin) is UNIQUE (uq_marine_import_file_hash_origin), so a
# re-import of the same bytes from the same origin cannot create a new row without a schema
# change — and it should not: one source file is one ledger entry PER ORIGIN, whose counters
# describe its LATEST processing.
#
# The row is NOT deleted and its id is preserved, so every core.marine_import_errors row
# and every core.port_craft / core.pilotage / core.sea_channel row that references
# import_file_id stays valid and attached to the same entry.
_FILE_REOPEN = """
UPDATE core.marine_import_files
   SET status = 'PENDING', total_rows = :total_rows, success_rows = 0, failed_rows = 0,
       duplicate_rows = 0, error_detail = NULL, filename = :filename,
       document_type = :document_type, uploaded_by = :uploaded_by, updated_at = now()
 WHERE id = :id
RETURNING id
"""

#: Row errors from the PREVIOUS run of this file. Cleared on override so the ledger shows
#: the errors of the run it currently reports, not a union of every attempt.
_CLEAR_FILE_ERRORS = "DELETE FROM core.marine_import_errors WHERE import_file_id = :id"

# core.vessel columns written by the VESPRO upsert (imo_no is the PK / conflict target).
_VESSEL_COLS = (
    "imo_no", "vessel_name", "call_sign", "flag", "vessel_type", "mtmv", "loa_m", "beam_m",
    "lbp_m", "max_draft_m", "grt", "nrt", "dwt", "teu_capacity", "mmsi", "engine_type",
    "num_engines", "propulsion_type", "num_propellers", "max_speed_kn", "bow_thruster",
    "stern_thruster", "built_date", "reg_port", "owner_name", "email", "vespro_ref",
)
# core.vessel_call columns bound by both call upserts (VCN and pre-VCN).
# `terminal_code` is NOT a column — it is the PCS code (CALINF DockORTOCode, or the BERMAN
# VCN infix) that both upserts resolve to terminal_id through ref_terminal(+alias).
_CALL_COLS = (
    "vcn", "via_no", "imo_no", "vessel_name", "voyage_no", "rotation_no", "purpose",
    "status", "eta", "etb", "etd", "ata", "atd", "atc", "source_note", "terminal_code",
    "berth_code",
)

# Resolve-or-NULL terminal lookup, mirroring the imo_no idiom below and the advance-list
# resolver in services/shipping_lines/repository.py: canonical code first, then the alias
# table (doc 01 §0 — four berth/terminal naming conventions make the alias table
# mandatory). An unknown code yields NULL, never a stub terminal row.
_TERMINAL_LOOKUP = """coalesce(
     (SELECT t.terminal_id FROM core.ref_terminal t WHERE t.code = upper(:terminal_code)),
     (SELECT ta.terminal_id FROM core.ref_terminal_alias ta WHERE ta.alias = upper(:terminal_code)))"""

# Same resolve-or-NULL shape for the berth BERALT allots. Codes carry four spellings
# across the corpus (doc 01 §0 key 7: 'CB05'/'CB-05', 'BMCT04'/'BMCT-4'), so the alias
# table is consulted second. An unallotted or unknown berth yields NULL, never a stub.
# Lifecycle ordering for core.vessel_call.status, so a RE-IMPORT cannot move a call
# BACKWARDS. Without this, replaying a BERMAN file over a call BERALT has already advanced
# rewrites 'Berth Allotted' back to 'Berth Planned' — the COALESCE takes whatever arrives
# last, and file order is not lifecycle order.
#
# Ranks the KNOWN vocabulary only (parsers/pcs_common.py). Anything else — legacy
# free-text, a CSV-template value, NULL — ranks 0, which gives the two safe behaviours:
#   * an unknown/absent incoming status never overwrites a known stage;
#   * a known stage may still upgrade a row that has no recognised stage yet.
# VESARR/VESDEP extend this list when their stages land; the column stays free-text
# (migration 0038 [D8]) so no constraint or migration is involved.
# Rendered from state_engine.STATUS_ORDER, so the SQL guard and the Python engine cannot
# drift apart. The ladder itself is defined once, in the engine.
_STATUS_ORDER = status_order_sql()
_STATUS_RANK_NEW = f"COALESCE(array_position({_STATUS_ORDER}, EXCLUDED.status), 0)"
_STATUS_RANK_OLD = f"COALESCE(array_position({_STATUS_ORDER}, core.vessel_call.status), 0)"

_BERTH_LOOKUP = """coalesce(
     (SELECT b.berth_id FROM core.ref_berth b WHERE b.code = upper(:berth_code)),
     (SELECT ba.berth_id FROM core.ref_berth_alias ba WHERE ba.alias = upper(:berth_code)))"""

_VESSEL_UPSERT = """
INSERT INTO core.vessel
    (imo_no, vessel_name, call_sign, flag, vessel_type, mtmv, loa_m, beam_m, lbp_m,
     max_draft_m, grt, nrt, dwt, teu_capacity, mmsi, engine_type, num_engines,
     propulsion_type, num_propellers, max_speed_kn, bow_thruster, stern_thruster,
     built_date, reg_port, owner_name, email, vespro_ref)
VALUES
    (:imo_no, :vessel_name, :call_sign, :flag, :vessel_type, :mtmv, :loa_m, :beam_m, :lbp_m,
     :max_draft_m, :grt, :nrt, :dwt, :teu_capacity, :mmsi, :engine_type, :num_engines,
     :propulsion_type, :num_propellers, :max_speed_kn, :bow_thruster, :stern_thruster,
     :built_date, :reg_port, :owner_name, :email, :vespro_ref)
ON CONFLICT (imo_no) DO UPDATE SET
    vessel_name     = COALESCE(EXCLUDED.vessel_name, core.vessel.vessel_name),
    call_sign       = COALESCE(EXCLUDED.call_sign, core.vessel.call_sign),
    flag            = COALESCE(EXCLUDED.flag, core.vessel.flag),
    vessel_type     = COALESCE(EXCLUDED.vessel_type, core.vessel.vessel_type),
    mtmv            = COALESCE(EXCLUDED.mtmv, core.vessel.mtmv),
    loa_m           = COALESCE(EXCLUDED.loa_m, core.vessel.loa_m),
    beam_m          = COALESCE(EXCLUDED.beam_m, core.vessel.beam_m),
    lbp_m           = COALESCE(EXCLUDED.lbp_m, core.vessel.lbp_m),
    max_draft_m     = COALESCE(EXCLUDED.max_draft_m, core.vessel.max_draft_m),
    grt             = COALESCE(EXCLUDED.grt, core.vessel.grt),
    nrt             = COALESCE(EXCLUDED.nrt, core.vessel.nrt),
    dwt             = COALESCE(EXCLUDED.dwt, core.vessel.dwt),
    teu_capacity    = COALESCE(EXCLUDED.teu_capacity, core.vessel.teu_capacity),
    mmsi            = COALESCE(EXCLUDED.mmsi, core.vessel.mmsi),
    engine_type     = COALESCE(EXCLUDED.engine_type, core.vessel.engine_type),
    num_engines     = COALESCE(EXCLUDED.num_engines, core.vessel.num_engines),
    propulsion_type = COALESCE(EXCLUDED.propulsion_type, core.vessel.propulsion_type),
    num_propellers  = COALESCE(EXCLUDED.num_propellers, core.vessel.num_propellers),
    max_speed_kn    = COALESCE(EXCLUDED.max_speed_kn, core.vessel.max_speed_kn),
    bow_thruster    = COALESCE(EXCLUDED.bow_thruster, core.vessel.bow_thruster),
    stern_thruster  = COALESCE(EXCLUDED.stern_thruster, core.vessel.stern_thruster),
    built_date      = COALESCE(EXCLUDED.built_date, core.vessel.built_date),
    reg_port        = COALESCE(EXCLUDED.reg_port, core.vessel.reg_port),
    owner_name      = COALESCE(EXCLUDED.owner_name, core.vessel.owner_name),
    email           = COALESCE(EXCLUDED.email, core.vessel.email),
    vespro_ref      = COALESCE(EXCLUDED.vespro_ref, core.vessel.vespro_ref)
RETURNING (xmax = 0) AS inserted
"""

_VESSEL_INSURANCE_UPSERT = """
INSERT INTO core.vessel_insurance (imo_no, pi_club, valid_until)
VALUES (:imo_no, :pi_club, :valid_until)
ON CONFLICT (imo_no, pi_club) DO UPDATE SET
    valid_until = COALESCE(EXCLUDED.valid_until, core.vessel_insurance.valid_until)
"""

# CALINF pre-VCN upsert: vcn is NULL, so the conflict target is the partial unique
# index uq_vessel_call_imo_voyage_pre_vcn (imo_no, voyage_no) WHERE vcn IS NULL.
_VESSEL_CALL_PREVCN_UPSERT = f"""
INSERT INTO core.vessel_call
    (vcn, via_no, imo_no, vessel_name, voyage_no, rotation_no, purpose, status,
     eta, etb, etd, ata, atd, atc, source_note, terminal_id, berth_id, data_origin)
VALUES
    (:vcn, :via_no,
     (SELECT v.imo_no FROM core.vessel v WHERE v.imo_no = :imo_no),
     :vessel_name, :voyage_no, :rotation_no, :purpose, :status,
     :eta, :etb, :etd, :ata, :atd, :atc, :source_note, {_TERMINAL_LOOKUP}, {_BERTH_LOOKUP},
     :data_origin)
ON CONFLICT (imo_no, voyage_no) WHERE vcn IS NULL DO UPDATE SET
    terminal_id = COALESCE(EXCLUDED.terminal_id, core.vessel_call.terminal_id),
    berth_id    = COALESCE(EXCLUDED.berth_id, core.vessel_call.berth_id),
    via_no      = COALESCE(EXCLUDED.via_no, core.vessel_call.via_no),
    vessel_name = COALESCE(EXCLUDED.vessel_name, core.vessel_call.vessel_name),
    rotation_no = COALESCE(EXCLUDED.rotation_no, core.vessel_call.rotation_no),
    purpose     = COALESCE(EXCLUDED.purpose, core.vessel_call.purpose),
    status      = CASE WHEN {_STATUS_RANK_NEW} >= {_STATUS_RANK_OLD}
                       THEN COALESCE(EXCLUDED.status, core.vessel_call.status)
                       ELSE core.vessel_call.status END,
    eta         = COALESCE(EXCLUDED.eta, core.vessel_call.eta),
    etb         = COALESCE(EXCLUDED.etb, core.vessel_call.etb),
    etd         = COALESCE(EXCLUDED.etd, core.vessel_call.etd),
    ata         = COALESCE(EXCLUDED.ata, core.vessel_call.ata),
    atd         = COALESCE(EXCLUDED.atd, core.vessel_call.atd),
    atc         = COALESCE(EXCLUDED.atc, core.vessel_call.atc),
    source_note = COALESCE(EXCLUDED.source_note, core.vessel_call.source_note)
RETURNING call_id, (xmax = 0) AS inserted
"""

# BERMAN promotion: stamp the VCN onto an existing pre-VCN seed for this (imo, voyage)
# so the subsequent VCN upsert enriches ONE row instead of creating a second. imo_no is
# resolved the same resolve-or-NULL way the seed stored it, so the match is consistent.
#
# THE VCN MUST NOT ALREADY BE TAKEN. `vcn` is UNIQUE (vessel_call_vcn_key), and this
# UPDATE assumes it is the only writer — that a pre-VCN seed for an (imo, voyage) is
# necessarily the row this VCN belongs to. That is false when a SECOND row already holds
# the VCN, which is the state 81 seed/holder pairs are in: the seed carries the CALINF
# schedule and the holder carries the CALINV/BERALT outcome, and they were never merged.
# Without the guard the UPDATE raises 23505 and, because persist() is ONE transaction,
# an entire journal import writes zero rows.
#
# With the guard the promote simply does not fire in that case, and the very next
# statement (_VESSEL_CALL_UPSERT, ON CONFLICT (vcn)) enriches the row that already holds
# the VCN — the correct destination. Where no row holds the VCN the guard is true and
# behaviour is byte-identical to before, so the merge that CALINF+BERMAN relies on is
# untouched. This makes a hard abort a no-op; it does NOT merge the pre-existing split
# pair, which is a data question and deliberately out of scope here.
_VESSEL_CALL_PROMOTE = """
UPDATE core.vessel_call
   SET vcn = :vcn
 WHERE vcn IS NULL
   AND voyage_no = :voyage_no
   AND imo_no IS NOT DISTINCT FROM (SELECT v.imo_no FROM core.vessel v WHERE v.imo_no = :imo_no)
   AND NOT EXISTS (SELECT 1 FROM core.vessel_call x WHERE x.vcn = :vcn)
"""

# CONFLICT TARGET IS INFERRED FROM COLUMNS, NOT NAMED.
# The rest of this repo names constraints explicitly, but core.vessel_call* is created by
# TWO different DDLs that name the SAME uniqueness differently — the boot DDL declares
# `CONSTRAINT uq_vessel_call_event UNIQUE (...)` while schema.sql (which built the
# deployed database) declares none at all. `ON CONFLICT ON CONSTRAINT <name>` therefore
# fails with "constraint does not exist" wherever the other DDL ran. Column inference
# matches ANY unique index over these columns, whatever it is called, so it survives both
# schemas and any future rename. See ensure_marine_schema for the index this relies on.
# berth_id carries the berth the milestone happened AT — BERALT's BERTH_ALLOTTED names the
# allotted berth, and VESARR's BERTHED will name the same one. Resolved the same
# resolve-or-NULL way as the call, so an unknown code stores NULL rather than failing.
#
# source_file is deliberately NOT written: its FK (fk_vessel_call_event_source_file) points
# at core.ingest_file(file_id), NOT the core.marine_import_files(id) this import path
# creates. Binding our ledger id there would violate the FK on every event — the same
# cross-object mistake that made ON CONFLICT ON CONSTRAINT fail in production. Wiring it
# needs an ingest_file row first, which is a separate provenance slice.
#
# data_origin (migration 0121) tags the milestone LIVE (API) or DEMO (MANUAL). It is
# deliberately NOT part of the conflict target: the uniqueness that exists on the deployed
# database is over the milestone triple alone, so adding it here would infer on an index
# that does not exist. The same milestone arriving from both corpora therefore lands once,
# keeping the tag the first ingest gave it.
_EVENT_INSERT = f"""
INSERT INTO core.vessel_call_event (call_id, event_type, event_ts, berth_id, data_origin)
VALUES (:call_id, :event_type, :event_ts, {_BERTH_LOOKUP}, :data_origin)
ON CONFLICT (call_id, event_type, event_ts) DO NOTHING
RETURNING event_id
"""

# Project the VESARR/VESDEP ACTUALS from the event ledger onto the call row, so the KPI
# aggregates in stats() (arrived / in_port / ops_completed / avg_turnaround /
# avg_pre_berth_delay) stop reading 0 and NULL. Those columns had NO producer before this:
# the actuals were written to core.vessel_call_event and never read back.
#
# NO SCHEMA CHANGE. Only the existing ata / atd columns are written:
#   * ata <- the ARRIVED milestone (VESARR DateTimeArrivalVessel)
#   * atd <- the DEPARTED milestone (VESDEP DateTimeOfDeparture) — the official ATD
# `atc` is deliberately untouched: no NLP Marine message carries cargo-complete, so
# Pre-Sailing Delay stays uncomputable and honestly NULL rather than guessed.
# `atb` is NOT introduced — BERTHED remains an event only, and no KPI formula changes.
#
# Derived from the LEDGER, not from the batch, so it is idempotent and self-healing: a
# re-import writes the same milestones (ON CONFLICT DO NOTHING), min() is stable, and a
# call whose events arrived across SEVERAL files still resolves correctly. The event value
# wins over whatever sat in the column, because the actuals ARE the source of truth;
# COALESCE keeps the existing value when the milestone is absent.
_PROJECT_CALL_ACTUALS = f"""
UPDATE core.vessel_call c
   SET ata = COALESCE(a.arrived,  c.ata),
       atd = COALESCE(a.departed, c.atd)
  FROM (SELECT min(event_ts) FILTER (WHERE event_type = '{EVENT_ARRIVED}')  AS arrived,
               min(event_ts) FILTER (WHERE event_type = '{EVENT_DEPARTED}') AS departed
          FROM core.vessel_call_event
         WHERE call_id = :call_id) a
 WHERE c.call_id = :call_id
"""

_ERROR_INSERT = """
INSERT INTO core.marine_import_errors (import_file_id, row_number, error_message, raw_data)
VALUES (:fid, :rn, :msg, :raw)
"""

_RESOLVE_BY_VCN = "SELECT call_id FROM core.vessel_call WHERE vcn = :vcn LIMIT 1"

# Tiebreak is `eta DESC, call_id DESC` — the SAME rule _RESOLVE_BY_VIA, the berthing
# LATERAL and MarineProjection.by_vias use, so every consumer that asks "which call" gets
# the same answer.
#
# It was `updated_at DESC`, which is an IDENTITY BUG rather than a tiebreak: updated_at
# moves every time any import touches a row, so re-running the same file could attach an
# event to a DIFFERENT call than the first run did. Identity must not depend on write
# order. `eta` and `call_id` are properties of the call itself and never move.
_RESOLVE_BY_IMO_VOYAGE = (
    "SELECT call_id FROM core.vessel_call WHERE imo_no = :imo_no AND voyage_no = :voyage_no "
    "ORDER BY eta DESC NULLS LAST, call_id DESC LIMIT 1"
)
_RESOLVE_BY_VIA = (
    "SELECT call_id FROM core.vessel_call WHERE via_no = :via_no "
    "ORDER BY eta DESC NULLS LAST, call_id DESC LIMIT 1"
)

# --------------------------------------------------------------------------- sea channel
# Geometry insert. name is NOT unique on core.sea_channel, so the dedup key is the
# content hash (uq_sea_channel_row). Geometry is GeoJSON (WGS84, reprojected at parse).
_SEA_CHANNEL_INSERT = """
INSERT INTO core.sea_channel
    (name, section_label, area_ha, length_m, geom_geojson, import_file_id, row_sha256,
     data_origin)
VALUES
    (:name, :section_label, :area_ha, :length_m, CAST(:geom_geojson AS jsonb),
     :import_file_id, :row_sha256, :data_origin)
ON CONFLICT (row_sha256) DO NOTHING
RETURNING channel_id
"""

# --------------------------------------------------------------------------- bathymetry
# Soundings arrive keyed by the survey's NATURAL key (drawing_no) — survey_id is a
# per-database identity surrogate and never crosses the wire. Phase 2: ensure the
# survey header exists (upsert on drawing_no) so a chart PDF/JSON import can land
# without a separate pre-registration step. Metadata-only columns are COALESCE-
# enriched so a re-import never nulls known title-block fields.
_RESOLVE_SURVEY_BY_DRAWING = (
    "SELECT survey_id FROM core.bathymetry_survey WHERE drawing_no = :drawing_no LIMIT 1"
)

_ENSURE_SURVEY = """
INSERT INTO core.bathymetry_survey
    (drawing_no, file_path, data_origin)
VALUES
    (:drawing_no, :file_path, :data_origin)
ON CONFLICT (drawing_no) DO UPDATE SET
    file_path = COALESCE(EXCLUDED.file_path, core.bathymetry_survey.file_path),
    -- LIVE/DEMO upload retags the survey so Overview under that toggle can see it
    -- (sounding hashes stay global; the header is what the list filter reads).
    data_origin = EXCLUDED.data_origin
RETURNING survey_id
"""

# Idempotent on the content hash (uq_bathymetry_sounding_row): a sounding has no natural
# key, so this follows core.sea_channel rather than the port_craft natural-key upsert.
# The hash is computed in the canonical model, so a sounding ingested from the chart PDF
# and the same sounding ingested from the JSON API collide correctly.
#
# Executed with executemany (one round trip per _BATHYMETRY_BATCH rows) because a single
# chart carries 15k-30k soundings and a per-row execute makes an import take hours. That is
# why there is NO `RETURNING` here: a multi-parameter execute cannot return rows, so the
# insert/duplicate split is established by _BATHYMETRY_EXISTING_HASHES below instead of by
# counting returned ids. ON CONFLICT DO NOTHING is retained as the concurrency backstop —
# the pre-filter answers "how many were new", the conflict clause guarantees correctness if
# a competing import inserts the same hash between the SELECT and the INSERT.
_BATHYMETRY_SOUNDING_INSERT = """
INSERT INTO core.bathymetry_sounding
    (survey_id, easting_m, northing_m, lat, lon, depth_m, above_design,
     page_x_pt, page_y_pt, import_file_id, row_sha256, data_origin)
VALUES
    (:survey_id, :easting_m, :northing_m, :lat, :lon, :depth_m, :above_design,
     :page_x_pt, :page_y_pt, :import_file_id, :row_sha256, :data_origin)
ON CONFLICT (row_sha256) DO NOTHING
"""

# Which of this batch's hashes are already stored. Indexed lookup on uq_bathymetry_sounding_row.
_BATHYMETRY_EXISTING_HASHES = (
    "SELECT row_sha256 FROM core.bathymetry_sounding WHERE row_sha256 = ANY(:hashes)"
)

#: Rows per executemany round trip. Large enough that a 30k-sounding chart is ~6 round
#: trips, small enough to keep the parameter payload well inside asyncpg's limits.
_BATHYMETRY_BATCH = 5000

# --------------------------------------------------------------------------- port craft
# Fleet-register upsert on the natural key `name`. All particulars are COALESCE-enriched
# (a re-import never nulls a known value); extras carries the raw parsed row.
_PORT_CRAFT_UPSERT = """
INSERT INTO core.port_craft
    (name, craft_type, owned_or_hired, owner_name, year_built, loa_m, breadth_m,
     draft_m, main_engines, bollard_pull_t, design_speed_kn, import_file_id, extras,
     data_origin)
VALUES
    (:name, :craft_type, :owned_or_hired, :owner_name, :year_built, :loa_m, :breadth_m,
     :draft_m, :main_engines, :bollard_pull_t, :design_speed_kn, :import_file_id,
     CAST(:extras AS jsonb), :data_origin)
ON CONFLICT (name) DO UPDATE SET
    craft_type      = COALESCE(EXCLUDED.craft_type, core.port_craft.craft_type),
    owned_or_hired  = COALESCE(EXCLUDED.owned_or_hired, core.port_craft.owned_or_hired),
    owner_name      = COALESCE(EXCLUDED.owner_name, core.port_craft.owner_name),
    year_built      = COALESCE(EXCLUDED.year_built, core.port_craft.year_built),
    loa_m           = COALESCE(EXCLUDED.loa_m, core.port_craft.loa_m),
    breadth_m       = COALESCE(EXCLUDED.breadth_m, core.port_craft.breadth_m),
    draft_m         = COALESCE(EXCLUDED.draft_m, core.port_craft.draft_m),
    main_engines    = COALESCE(EXCLUDED.main_engines, core.port_craft.main_engines),
    bollard_pull_t  = COALESCE(EXCLUDED.bollard_pull_t, core.port_craft.bollard_pull_t),
    design_speed_kn = COALESCE(EXCLUDED.design_speed_kn, core.port_craft.design_speed_kn),
    import_file_id  = EXCLUDED.import_file_id,
    extras          = EXCLUDED.extras
RETURNING (xmax = 0) AS inserted
"""

# --------------------------------------------------------------------------- pilotage
# Pilot roster upsert (core.pilot). name is COALESCE-enriched, never nulled.
_PILOT_UPSERT = """
INSERT INTO core.pilot (pilot_code, name)
VALUES (:pilot_code, :name)
ON CONFLICT (pilot_code) DO UPDATE SET name = COALESCE(EXCLUDED.name, core.pilot.name)
RETURNING (xmax = 0) AS inserted
"""

# Pilotage movement insert (core.pilotage). Resolve-or-NULL for the FK columns:
#   pilot_code   -> core.pilot        (upserted just above)
#   from/to_berth-> core.ref_berth    (by berth code; unresolved -> NULL, raw kept in extras)
#   call_id      -> core.vessel_call  (best-effort by VIA; unresolved -> NULL, no stub)
# Idempotent on the content hash: a byte-identical row collapses (uq_pilotage_row).
_PILOTAGE_INSERT = """
INSERT INTO core.pilotage
    (movement_type, call_id, via_no, imo_no, vessel_name, pilot_code, vessel_condition,
     from_berth_id, to_berth_id, draft_fwd_m, draft_aft_m,
     pilot_boarded_at, first_line_at, all_fast_at, pilot_disembarked_at, berth_vacated_at,
     anchor_down_at, anchor_up_at, submitted_at, extras, import_file_id, row_sha256,
     data_origin)
VALUES
    (:movement_type,
     (SELECT call_id FROM core.vessel_call WHERE via_no = :via_no
        ORDER BY eta DESC NULLS LAST, call_id DESC LIMIT 1),
     :via_no, :imo_no, :vessel_name,
     (SELECT pilot_code FROM core.pilot WHERE pilot_code = :pilot_code),
     :vessel_condition,
     (SELECT berth_id FROM core.ref_berth WHERE code = :from_berth_code),
     (SELECT berth_id FROM core.ref_berth WHERE code = :to_berth_code),
     :draft_fwd_m, :draft_aft_m,
     :pilot_boarded_at, :first_line_at, :all_fast_at, :pilot_disembarked_at, :berth_vacated_at,
     :anchor_down_at, :anchor_up_at, :submitted_at, CAST(:extras AS jsonb),
     :import_file_id, :row_sha256, :data_origin)
ON CONFLICT (row_sha256) DO NOTHING
RETURNING pilotage_id
"""

# VCN upsert. The conflict target is INFERRED from (vcn) rather than named: the uniqueness
# is `uq_vessel_call_vcn` under the boot DDL but `vessel_call_vcn_key` on the deployed
# database (schema.sql declares an inline `vcn text UNIQUE`, which PostgreSQL auto-names).
# Naming it broke every BERMAN/BERALT upload with "constraint does not exist"; inference
# matches either. See the note on _EVENT_INSERT.
#
# imo_no and terminal_id are both resolve-or-NULL, so the NOT VALID
# fk_vessel_call_imo / fk_vessel_call_terminal are always satisfied. berth_id is still
# unset here: BERMAN is the berth APPLICATION and carries no berth field — the allotment
# arrives with BERALT (not yet ingested). updated_at is set by the
# trg_vessel_call_updated_at trigger (migration 0038).
_VESSEL_CALL_UPSERT = f"""
INSERT INTO core.vessel_call
    (vcn, via_no, imo_no, vessel_name, voyage_no, rotation_no, purpose, status,
     eta, etb, etd, ata, atd, atc, source_note, terminal_id, berth_id, data_origin)
VALUES
    (:vcn, :via_no,
     (SELECT v.imo_no FROM core.vessel v WHERE v.imo_no = :imo_no),
     :vessel_name, :voyage_no, :rotation_no, :purpose, :status,
     :eta, :etb, :etd, :ata, :atd, :atc, :source_note, {_TERMINAL_LOOKUP}, {_BERTH_LOOKUP},
     :data_origin)
ON CONFLICT (vcn) DO UPDATE SET
    terminal_id = COALESCE(EXCLUDED.terminal_id, core.vessel_call.terminal_id),
    berth_id    = COALESCE(EXCLUDED.berth_id, core.vessel_call.berth_id),
    via_no      = COALESCE(EXCLUDED.via_no, core.vessel_call.via_no),
    imo_no      = COALESCE(EXCLUDED.imo_no, core.vessel_call.imo_no),
    vessel_name = COALESCE(EXCLUDED.vessel_name, core.vessel_call.vessel_name),
    voyage_no   = COALESCE(EXCLUDED.voyage_no, core.vessel_call.voyage_no),
    rotation_no = COALESCE(EXCLUDED.rotation_no, core.vessel_call.rotation_no),
    purpose     = COALESCE(EXCLUDED.purpose, core.vessel_call.purpose),
    status      = CASE WHEN {_STATUS_RANK_NEW} >= {_STATUS_RANK_OLD}
                       THEN COALESCE(EXCLUDED.status, core.vessel_call.status)
                       ELSE core.vessel_call.status END,
    eta         = COALESCE(EXCLUDED.eta, core.vessel_call.eta),
    etb         = COALESCE(EXCLUDED.etb, core.vessel_call.etb),
    etd         = COALESCE(EXCLUDED.etd, core.vessel_call.etd),
    ata         = COALESCE(EXCLUDED.ata, core.vessel_call.ata),
    atd         = COALESCE(EXCLUDED.atd, core.vessel_call.atd),
    atc         = COALESCE(EXCLUDED.atc, core.vessel_call.atc),
    source_note = COALESCE(EXCLUDED.source_note, core.vessel_call.source_note)
RETURNING call_id, (xmax = 0) AS inserted
"""
