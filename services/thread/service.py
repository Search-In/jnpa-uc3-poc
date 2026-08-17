"""The lifecycle traversal itself.

Hops are declared as data (`_HOPS`), not written as bespoke SQL per endpoint, so
that adding a source is one row and the honesty rule cannot be forgotten: the
runner visits EVERY declared hop and records a verdict for each, whether or not
it found anything.

Read-only by construction — `_read()` refuses anything that is not a SELECT, the
same guard `services/cargo/simulation/repository.py` applies to the what-if
engine, and every statement runs on `engine.connect()` so there is no transaction
to commit even if one slipped through.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from sqlalchemy import text

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

log = get_logger("services.thread")

_READ_ONLY = re.compile(r"^\s*(select|with)\b", re.I)
_WRITE_VERB = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|merge|copy|vacuum|refresh)\b",
    re.I)


class ThreadWriteAttempt(RuntimeError):
    """Raised if a traversal statement is anything but a read."""


#: Lifecycle stage a hop belongs to. IMPORT and EXPORT are the two directions the
#: JNPA process twin is scored on; SHARED hops appear in both.
IMPORT, EXPORT, SHARED = "IMPORT", "EXPORT", "SHARED"


@dataclass(frozen=True)
class HopSpec:
    """One source of truth for one step of a container's life."""
    key: str
    label: str
    stage: str
    table: str
    container_col: str
    #: Column carrying the truck/trailer, when this source records one.
    vehicle_col: Optional[str] = None
    #: Columns worth surfacing verbatim as the hop's evidence.
    detail_cols: Sequence[str] = ()
    #: Timestamp that dates the hop, if any.
    ts_col: Optional[str] = None
    #: The corpus file family behind the table, for the evidence trail.
    source: str = ""
    #: Column carrying this table's provenance. ALWAYS selected and ALWAYS
    #: surfaced, so a synthetic row can never be rendered as if it were corpus
    #: evidence — the failure this whole traversal exists to prevent.
    provenance_col: Optional[str] = None

    @property
    def is_corpus(self) -> bool:
        """Is this hop backed by a JNPA document, or by our own telemetry?

        Every corpus-backed source names a numbered corpus group ("8-Form13,
        EIR, PIN"); the simulator-fed tables say "UC-III ..." or "derived ...".
        The distinction decides which containers are worth OFFERING as examples:
        ranked by raw source count, the simulator tables — which name tens of
        thousands of boxes — bury every document-evidenced chain in the corpus.
        """
        return bool(self.source) and self.source[0].isdigit()


#: The canonical order a box moves through the port. Declared once; every
#: traversal walks all of it.
_HOPS: tuple[HopSpec, ...] = (
    HopSpec("manifest", "Manifested on IGM", IMPORT, "core.igm_line_container",
            "container_no", None, ("igm_no", "line_no", "seal_no", "iso_code", "status"),
            None, "1-NLP Marine/IGM · 5-Customs/IGM (CHPOI03)", provenance_col="data_origin"),
    HopSpec("advance_list", "Advance list (IAL/EAL)", SHARED, "core.advance_list_container",
            "container_no", None, ("vessel_visit", "out_vessel_visit", "voyage"),
            None, "4-Shipping Lines", provenance_col="data_origin"),
    HopSpec("edi_move", "EDI vessel move (COARRI/COPRAR)", SHARED, "core.edi_vessel_container",
            "container_no", None, ("vcn", "direction", "doc_type", "pol", "pod", "seal_no"),
            "shipping_ts", "6-EDI Message and Format", provenance_col="data_origin"),
    HopSpec("rms_scan", "RMS scan selection", IMPORT, "core.rms_scan_container",
            "container_no", None, ("igm_no", "machine_code"), None, "5-Customs/RMS", provenance_col="data_origin"),
    HopSpec("customs_ooc", "Customs out-of-charge", IMPORT, "core.ooc_item",
            "container_no", None, ("be_no",), None, "5-Customs/OOC (CHPOI10)", provenance_col="data_origin"),
    HopSpec("delivery_order", "Electronic delivery order", IMPORT, "core.delivery_order_line",
            "container_no", "vehicle_no", ("do_number", "vcn", "imo_number", "seal_no"),
            "arrival_ts", "4-Shipping Lines/EDO", provenance_col="data_origin"),
    HopSpec("smtp", "SMTP transhipment permit", IMPORT, "core.smtp_container",
            "container_no", None, ("smtp_no", "container_type"), None, "5-Customs/SMTP (CHPOI13)", provenance_col="data_origin"),
    HopSpec("pin_ticket", "PIN pickup ticket", IMPORT, "core.pin_ticket",
            "container_number", "truck_no", ("pin_number", "terminal", "yard_location", "company"),
            "issued_at", "8-Form13, EIR, PIN", provenance_col="data_origin"),
    HopSpec("eir", "Equipment interchange report", SHARED, "core.eir",
            "container_number", "truck_no", ("eir_no", "terminal", "vessel", "via_no",
                                             "driver_name", "tat_minutes"),
            "truck_in_time", "8-Form13, EIR, PIN", provenance_col="data_origin"),
    HopSpec("gate_document", "Gate document (parsed corpus)", SHARED, "core.gate_document",
            "container_no", "vehicle_no", ("doc_category", "doc_ref", "vessel_name", "voyage",
                                           "driver_name", "transporter_name", "gate_no"),
            "doc_ts", "8-Form13, EIR, PIN (parsed + scans)", provenance_col="data_origin"),
    HopSpec("codeco", "CODECO gate movement", SHARED, "core.codeco_movement",
            "container_no", "vehicle_no", ("vcn", "imo_no", "gate_pass_no", "gate_no",
                                           "delivery_mode", "iso_code"),
            "gate_pass_ts", "6-EDI Message and Format/CODECO", provenance_col="data_origin"),
    HopSpec("gate_event", "Gate crossing event", SHARED, "core.gate_event",
            "container_number", "plate", ("gate_id", "event_type", "trip_id"),
            "ts", "UC-III gate telemetry", provenance_col="source"),
    HopSpec("job", "Truck job assignment", SHARED, "core.container_job_assignment",
            "container_number", "vehicle_no", ("move_type", "document_type", "terminal", "status"),
            "assigned_at", "UC-III lifecycle", provenance_col="assigned_by"),
    HopSpec("yard_move", "Yard movement", SHARED, "core.cargo_movement_event",
            "container_number", "vehicle_no", ("movement_type", "yard_location", "terminal"),
            "occurred_at", "UC-III lifecycle", provenance_col="actor"),
    HopSpec("cfs_ecy", "CFS / empty-yard movement", SHARED, "core.cfs_ecy_movement",
            "container_number", None, ("facility_type", "mode"), "event_ts", "13-CFS-ECY", provenance_col="data_origin"),
    HopSpec("rail_cto", "Rail rake manifest (CTO)", SHARED, "core.cto_manifest_entry",
            "container_no", None, ("rake_id", "wagon_no", "origin_icd", "terminal"),
            "event_ts", "10-Form 11_ICD Rail/CTO", provenance_col="data_origin"),
    HopSpec("rail_form11", "Rail export pre-advice (Form 11)", EXPORT, "core.form11_entry",
            "container_no", None, ("terminal", "via", "icd_location", "pod", "booking_number"),
            None, "10-Form 11_ICD Rail/Form 11", provenance_col="data_origin"),
    HopSpec("cargo", "Shared cargo record", SHARED, "core.cargo",
            "container_number", "vehicle_number", ("vessel_name", "customs_status", "yard_block",
                                                   "lifecycle_status", "origin_stream"),
            "eta", "derived — UC-II/UC-III shared record", provenance_col="origin_stream"),
)

_HOP_BY_KEY = {h.key: h for h in _HOPS}


@dataclass
class Hop:
    key: str
    label: str
    stage: str
    #: FOUND when the corpus evidences this step; NOT_IN_CORPUS when nothing does.
    verdict: str
    source_table: str
    source_files: str
    rows: List[Dict[str, Any]] = field(default_factory=list)
    vehicles: List[str] = field(default_factory=list)
    note: Optional[str] = None
    #: Distinct provenance values across this hop's rows, e.g. ['REAL'] or
    #: ['SYNTHETIC:flow-v1']. Surfaced at the top level so a caller never has to
    #: dig through row dictionaries to discover a step was fabricated.
    provenance: List[str] = field(default_factory=list)

    @property
    def is_synthetic(self) -> bool:
        return any("SYNTH" in str(p).upper() or str(p).upper() == "SIM"
                   for p in self.provenance)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "hop": self.key, "label": self.label, "stage": self.stage,
            "verdict": self.verdict, "source_table": self.source_table,
            "source_files": self.source_files, "row_count": len(self.rows),
            "provenance": self.provenance, "synthetic": self.is_synthetic,
            "rows": self.rows, "vehicles": self.vehicles, "note": self.note,
        }


@dataclass
class ThreadResult:
    subject: Dict[str, Any]
    hops: List[Hop]
    vehicles: List[Dict[str, Any]]
    queries: List[Dict[str, Any]]
    summary: Dict[str, Any]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "subject": self.subject,
            "summary": self.summary,
            "hops": [h.as_dict() for h in self.hops],
            "vehicles": self.vehicles,
            # Notice §1(d): the working must be traceable to the queries that
            # produced it.
            "queries": self.queries,
        }


class ContainerThreadService:
    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    # ------------------------------------------------------------------ guard
    @staticmethod
    def assert_read_only(sql: str) -> None:
        if not _READ_ONLY.match(sql or "") or _WRITE_VERB.search(sql or ""):
            raise ThreadWriteAttempt(f"traversal statements must be reads: {sql[:120]!r}")

    async def _read(self, conn, sql: str, params: Mapping[str, Any],
                    trace: List[Dict[str, Any]], label: str
                    ) -> tuple[List[Dict[str, Any]], Optional[str]]:
        """Run one hop's read. Returns (rows, error).

        The error is returned, NOT swallowed, because a failed query and an empty
        result mean opposite things: "we could not look" versus "we looked and the
        corpus has nothing". Reporting the first as the second is the exact
        dishonesty this traversal exists to avoid.

        A failed statement also aborts the enclosing transaction — every
        subsequent hop then dies with InFailedSQLTransactionError and, before this
        rollback was added, was reported as NOT_IN_CORPUS. One bad column name
        silently emptied the entire thread.
        """
        self.assert_read_only(sql)
        err: Optional[str] = None
        rows: List[Dict[str, Any]] = []
        try:
            res = await conn.execute(text(sql), dict(params))
            rows = [dict(r) for r in res.mappings().all()]
        except Exception as exc:
            err = str(exc).splitlines()[0][:160]
            try:
                await conn.rollback()  # release the aborted transaction
            except Exception:  # pragma: no cover — connection already gone
                pass
        trace.append({"hop": label, "sql": " ".join(sql.split()), "params": dict(params),
                      "row_count": len(rows), "error": err})
        return rows, err

    async def _existing_columns(self, conn, trace: List[Dict[str, Any]]) -> Dict[str, set]:
        """Actual columns per hop table.

        The hop specs name the fields worth surfacing, but a spec is written by
        hand and the schema is not: `core.rms_scan_container` has no
        `machine_code`, and selecting it took the whole traversal down. Intersect
        the declaration with reality and carry on with what exists.
        """
        names = sorted({h.table.split(".", 1)[1] for h in _HOPS})
        rows, _err = await self._read(
            conn,
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'core' AND table_name = ANY(:t)",
            {"t": names}, trace, "schema_introspect")
        out: Dict[str, set] = {}
        for r in rows:
            out.setdefault(f"core.{r['table_name']}", set()).add(r["column_name"])
        return out

    # ------------------------------------------------------- container thread
    async def container_thread(self, container_no: str, *,
                               row_limit: int = 20) -> ThreadResult:
        cn = (container_no or "").strip().upper()
        trace: List[Dict[str, Any]] = []
        hops: List[Hop] = []
        plates: set[str] = set()

        async with get_engine(self._dsn).connect() as conn:
            schema = await self._existing_columns(conn, trace)
            for spec in _HOPS:
                have = schema.get(spec.table)
                if have is not None and spec.container_col not in have:
                    hops.append(Hop(
                        key=spec.key, label=spec.label, stage=spec.stage, verdict="ERROR",
                        source_table=spec.table, source_files=spec.source,
                        note=f"{spec.table} has no column {spec.container_col} — hop spec "
                             f"is out of step with the schema"))
                    continue

                wanted = [spec.container_col, *spec.detail_cols]
                if spec.vehicle_col:
                    wanted.append(spec.vehicle_col)
                if spec.provenance_col:
                    wanted.append(spec.provenance_col)
                ts = spec.ts_col if (have is None or spec.ts_col in (have or ())) else None
                if ts:
                    wanted.append(ts)
                # Keep only columns this table really has (order preserved).
                cols = [c for c in dict.fromkeys(wanted) if have is None or c in have]
                select = ", ".join(cols)
                order = f" ORDER BY {ts} NULLS LAST" if ts else ""
                sql = (f"SELECT {select} FROM {spec.table} "
                       f"WHERE upper({spec.container_col}) = :cn{order} LIMIT {int(row_limit)}")
                rows, err = await self._read(conn, sql, {"cn": cn}, trace, spec.key)

                vehicles: List[str] = []
                if spec.vehicle_col and spec.vehicle_col in cols:
                    for r in rows:
                        v = r.get(spec.vehicle_col)
                        if v:
                            v = str(v).strip().upper()
                            vehicles.append(v)
                            plates.add(v)
                if err:
                    verdict, note = "ERROR", f"query failed: {err}"
                elif rows:
                    verdict, note = "FOUND", None
                else:
                    verdict, note = ("NOT_IN_CORPUS",
                                     f"searched {spec.table}; the corpus does not evidence "
                                     f"this step for this container")
                prov = sorted({str(r.get(spec.provenance_col)) for r in rows
                               if spec.provenance_col and r.get(spec.provenance_col)})
                hops.append(Hop(
                    key=spec.key, label=spec.label, stage=spec.stage, verdict=verdict,
                    source_table=spec.table, source_files=spec.source,
                    rows=rows, vehicles=sorted(set(vehicles)), note=note,
                    provenance=prov))

            vehicles = await self._vehicles(conn, sorted(plates), trace)

        found = [h for h in hops if h.verdict == "FOUND"]
        errored = [h for h in hops if h.verdict == "ERROR"]
        summary = {
            "container_no": cn,
            "hops_total": len(hops),
            "hops_found": len(found),
            # Kept apart on purpose: "the corpus has nothing here" and "we could
            # not look here" are different answers and must never be merged.
            "hops_not_in_corpus": len(hops) - len(found) - len(errored),
            "hops_errored": len(errored),
            "reaches_a_vehicle": bool(plates),
            "vehicle_count": len(plates),
            "stages_found": sorted({h.stage for h in found}),
            # If ANY hop on this chain is fabricated, say so at the top. A caller
            # that reads only the summary must still learn it.
            "has_synthetic_hops": any(h.is_synthetic for h in found),
            "synthetic_hops": [h.key for h in found if h.is_synthetic],
        }
        return ThreadResult(subject={"type": "container", "container_no": cn},
                            hops=hops, vehicles=vehicles, queries=trace, summary=summary)

    # --------------------------------------------------------- vehicle lookup
    async def _vehicles(self, conn, plates: Sequence[str],
                        trace: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Resolve each plate to its transporter and driver, carrying the
        provenance so an assumed mapping is never shown as an evidenced one."""
        if not plates:
            return []
        sql = ("SELECT tv.vehicle_no_norm AS plate, tv.provenance, tv.assumption_ref, "
               "       tv.source_ref, t.company_name AS transporter, d.driver_name, "
               "       d.licence_no_norm AS driver_licence "
               "  FROM core.transporter_vehicle tv "
               "  LEFT JOIN core.transporter t ON t.id = tv.transporter_id "
               "  LEFT JOIN core.driver d ON d.driver_id::text = tv.driver_id "
               " WHERE tv.vehicle_no_norm = ANY(:plates)")
        rows, _err = await self._read(conn, sql, {"plates": list(plates)}, trace, "vehicle_registry")
        by = {r["plate"]: r for r in rows}
        out = []
        for p in plates:
            r = by.get(p)
            out.append(r or {"plate": p, "provenance": None, "transporter": None,
                             "driver_name": None,
                             "note": "plate appears on a document but resolves to no "
                                     "transporter — corpus gap G6"})
        return out

    # --------------------------------------------------------- vehicle thread
    async def vehicle_thread(self, plate: str) -> Dict[str, Any]:
        """Everything one truck carried, across every source that records a plate.

        The vehicle key is spelled four different ways across the estate
        (`vehicle_no`, `truck_no`, `plate`, `vehicle_number`), so this walks each
        hop that has one rather than assuming a single column.
        """
        p = (plate or "").strip().upper()
        trace: List[Dict[str, Any]] = []
        sightings: List[Dict[str, Any]] = []
        containers: set[str] = set()

        async with get_engine(self._dsn).connect() as conn:
            schema = await self._existing_columns(conn, trace)
            for spec in _HOPS:
                if not spec.vehicle_col:
                    continue
                have = schema.get(spec.table)
                if have is not None and (spec.vehicle_col not in have
                                         or spec.container_col not in have):
                    continue
                cols = [c for c in dict.fromkeys(
                    [spec.container_col, spec.vehicle_col, *spec.detail_cols,
                     *( [spec.ts_col] if spec.ts_col else [] )])
                    if have is None or c in have]
                sql = (f"SELECT {', '.join(cols)} FROM {spec.table} "
                       f"WHERE upper({spec.vehicle_col}) = :p LIMIT 100")
                rows, err = await self._read(conn, sql, {"p": p}, trace, f"veh:{spec.key}")
                for r in rows:
                    cn = r.get(spec.container_col)
                    if cn:
                        containers.add(str(cn).strip().upper())
                if rows or err:
                    sightings.append({"hop": spec.key, "label": spec.label,
                                      "source_table": spec.table,
                                      "verdict": "ERROR" if err else "FOUND",
                                      "row_count": len(rows), "rows": rows,
                                      "note": f"query failed: {err}" if err else None})
            registry = await self._vehicles(conn, [p], trace)

        return {
            "subject": {"type": "vehicle", "plate": p},
            "summary": {"sources_hit": [s["hop"] for s in sightings if s["verdict"] == "FOUND"],
                        "distinct_containers": len(containers)},
            "registry": registry[0] if registry else None,
            "containers": sorted(containers),
            "sightings": sightings,
            "queries": trace,
        }

    # ---------------------------------------------------------- vessel thread
    async def vessel_containers(self, *, vessel_name: Optional[str] = None,
                                vcn: Optional[str] = None, via_no: Optional[str] = None,
                                imo_no: Optional[str] = None,
                                limit: int = 500) -> Dict[str, Any]:
        """Every container this vessel call touched, from every source that
        records a vessel, each tagged with where it came from."""
        trace: List[Dict[str, Any]] = []
        sources: List[Dict[str, Any]] = []
        norm = lambda s: (s or "").strip().upper()

        async with get_engine(self._dsn).connect() as conn:
            if vessel_name or imo_no:
                sql = ("SELECT DISTINCT c.container_no, i.igm_no, i.imo_no, v.vessel_name "
                       "  FROM core.igm_line_container c "
                       "  JOIN core.igm i ON i.igm_no = c.igm_no "
                       "  LEFT JOIN core.vessel v ON v.imo_no = i.imo_no "
                       " WHERE (CAST(:imo AS text) IS NOT NULL AND i.imo_no = :imo) "
                       "    OR (CAST(:vn AS text) IS NOT NULL AND upper(v.vessel_name) = :vn) "
                       f" LIMIT {int(limit)}")
                rows, _e = await self._read(conn, sql, {"imo": imo_no, "vn": norm(vessel_name) or None},
                                        trace, "manifest_by_vessel")
                sources.append({"source": "core.igm_line_container", "hop": "manifest",
                                "count": len(rows), "error": _e,
                                "containers": [r["container_no"] for r in rows]})

            if vcn:
                for tbl, col in (("core.codeco_movement", "vcn"),
                                 ("core.edi_vessel_container", "vcn"),
                                 ("core.delivery_order_line", "vcn")):
                    sql = (f"SELECT DISTINCT container_no FROM {tbl} "
                           f"WHERE upper({col}) = :vcn LIMIT {int(limit)}")
                    rows, _e = await self._read(conn, sql, {"vcn": norm(vcn)}, trace, f"by_vcn:{tbl}")
                    sources.append({"source": tbl, "hop": "vcn", "count": len(rows),
                                    "error": _e,
                                    "containers": [r["container_no"] for r in rows]})

            if via_no or vessel_name:
                sql = ("SELECT DISTINCT container_no, doc_category, doc_ref, vessel_name, voyage "
                       "  FROM core.gate_document "
                       " WHERE (CAST(:via AS text) IS NOT NULL AND "
                       "        (upper(btrim(voyage)) LIKE '%' || :via || '%' "
                       "         OR upper(btrim(vessel_name)) LIKE '%' || :via || '%')) "
                       "    OR (CAST(:vn AS text) IS NOT NULL AND vessel_name ILIKE '%' || :vn || '%') "
                       f" LIMIT {int(limit)}")
                rows, _e = await self._read(conn, sql,
                                        {"via": norm(via_no) or None, "vn": vessel_name or None},
                                        trace, "gate_docs_by_vessel")
                sources.append({"source": "core.gate_document", "hop": "gate_document",
                                "count": len(rows), "error": _e,
                                "containers": [r["container_no"] for r in rows if r["container_no"]]})

            sql = ("SELECT container_number FROM core.cargo "
                   "WHERE upper(regexp_replace(btrim(vessel_name), '\\s+', ' ', 'g')) = "
                   "      upper(regexp_replace(btrim(:vn), '\\s+', ' ', 'g')) "
                   f"LIMIT {int(limit)}")
            if vessel_name:
                rows, _e = await self._read(conn, sql, {"vn": vessel_name}, trace, "cargo_by_vessel")
                sources.append({"source": "core.cargo", "hop": "cargo", "count": len(rows),
                                "error": _e,
                                "containers": [r["container_number"] for r in rows]})

        allc: set[str] = set()
        for s in sources:
            allc.update(s["containers"])
        return {
            "subject": {"type": "vessel", "vessel_name": vessel_name, "vcn": vcn,
                        "via_no": via_no, "imo_no": imo_no},
            "summary": {"distinct_containers": len(allc),
                        "sources_hit": [s["source"] for s in sources if s["count"]],
                        # A source that ERRORED is not a source that found nothing.
                        "sources_errored": [s["source"] for s in sources if s.get("error")]},
            "sources": sources,
            "containers": sorted(allc),
            "queries": trace,
        }

    # ------------------------------------------------------------------ subjects
    async def subjects(self, *, limit: int = 5, stage: Optional[str] = None
                       ) -> Dict[str, Any]:
        """Containers whose chains actually resolve, ranked by coverage.

        UC-2's import board previously offered five hardcoded "hero" containers.
        The list was right when it was written, but it was a claim about the
        database frozen into the frontend: after any ingest it could name a box
        whose chain no longer resolves, or keep silent about a better one, and
        nothing would say so.

        This computes the same thing from the data — how many distinct sources
        name each container, and which. Offering examples at all is necessary
        rather than decorative: over a corpus this disjoint most container
        numbers legitimately return nothing, and a viewer cannot tell that from
        a broken lookup.

        Ordering is (source count, then container number) so the result is
        deterministic — an example list that reshuffles between page loads reads
        as instability.
        """
        trace: List[Dict[str, Any]] = []
        async with get_engine(self._dsn).connect() as conn:
            available = await self._existing_columns(conn, trace)

            # One SELECT per source, UNIONed: which containers does it name?
            parts, params = [], {}
            for hop in _HOPS:
                cols = available.get(hop.table)
                if not cols or hop.container_col not in cols:
                    continue
                parts.append(
                    f"SELECT DISTINCT upper(btrim({hop.container_col})) AS cn, "
                    f"'{hop.key}' AS hop, '{hop.stage}' AS stage, "
                    f"{'true' if hop.is_corpus else 'false'} AS is_corpus "
                    f"FROM {hop.table} "
                    f"WHERE {hop.container_col} IS NOT NULL "
                    f"AND btrim({hop.container_col}) <> ''")
            if not parts:
                return {"subjects": [], "queries": trace,
                        "note": "no source table with a container column is present"}

            stage_filter = ""
            if stage:
                stage_filter = "WHERE stage = :stage OR stage = 'SHARED'"
                params["stage"] = stage
            # Ranked by DOCUMENT-evidenced sources first. Ranking by raw source
            # count instead returns whichever boxes the simulator happens to have
            # touched most, because core.gate_event and core.cargo_movement_event
            # name tens of thousands of containers the corpus says nothing about
            # — the opposite of a worked example.
            sql = (f"WITH named AS ({' UNION ALL '.join(parts)}) "
                   f"SELECT cn, "
                   f"       count(DISTINCT hop) FILTER (WHERE is_corpus) AS corpus_sources, "
                   f"       count(DISTINCT hop) AS sources, "
                   f"       array_agg(DISTINCT hop ORDER BY hop) "
                   f"         FILTER (WHERE is_corpus) AS corpus_hops, "
                   f"       array_agg(DISTINCT hop ORDER BY hop) AS hops "
                   f"FROM named {stage_filter} "
                   f"GROUP BY cn "
                   f"HAVING count(DISTINCT hop) FILTER (WHERE is_corpus) > 0 "
                   f"ORDER BY count(DISTINCT hop) FILTER (WHERE is_corpus) DESC, "
                   f"         count(DISTINCT hop) DESC, cn ASC "
                   f"LIMIT :limit")
            # Over-fetch: the selection below is a coverage problem, not a
            # top-N one, so it needs a pool to choose from.
            params["limit"] = max(limit * 40, 200)
            rows, error = await self._read(conn, sql, params, trace, "subjects")

        labels = {h.key: h.label for h in _HOPS}

        # Greedy set cover over the document-evidenced hops.
        #
        # Ranking by coverage alone returns N boxes that all demonstrate the
        # SAME few hops — measured here, five containers each showing exactly
        # IGM + EDI + CFS, which teaches a viewer nothing about the other
        # fifteen steps. What makes a set of examples useful is that between
        # them they reach every stage that any document reaches, which is what
        # the hand-written list it replaces was chosen for. So: take the box
        # with the most document hops, then repeatedly take whichever box adds
        # the most hops nobody has shown yet, falling back to raw coverage once
        # nothing new is left to add.
        pool = [(r["cn"], set(r["corpus_hops"] or ()), r["corpus_sources"],
                 r["sources"], list(r["hops"] or ())) for r in rows]
        chosen, covered = [], set()
        while pool and len(chosen) < limit:
            best = max(pool, key=lambda p: (len(p[1] - covered), p[2], -ord(p[0][0])))
            pool.remove(best)
            chosen.append(best)
            covered |= best[1]
        rows = [{"cn": c[0], "corpus_hops": sorted(c[1]), "corpus_sources": c[2],
                 "sources": c[3], "hops": c[4]} for c in chosen]

        subjects = [{
            "container_no": r["cn"],
            # Kept separate on purpose: "5 sources" and "5 JNPA documents" are
            # different claims, and only the second is evidence.
            "corpus_source_count": r["corpus_sources"],
            "source_count": r["sources"],
            "corpus_hops": list(r["corpus_hops"] or ()),
            "hops": list(r["hops"] or ()),
            # What this box demonstrates, in the words of the sources that name
            # it — never a hand-written blurb that can drift from the data.
            "covers": " → ".join(labels.get(h, h)
                                 for h in (r["corpus_hops"] or ())),
        } for r in rows]
        return {"subjects": subjects, "queries": trace,
                "error": error,
                "note": ("Ranked by how many JNPA DOCUMENTS name the box, not by "
                         "total sources — the simulator-fed tables name tens of "
                         "thousands of containers the corpus is silent about. "
                         "Chosen so that BETWEEN them the examples reach every "
                         "stage any document reaches. Computed live: this list "
                         "follows the database, it is not a fixed set.")}
