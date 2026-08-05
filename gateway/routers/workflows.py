"""/api/workflows — a minimum-viable automation rule composer (audit closure).

Lets an operator author IF/THEN automation rules WITHOUT code changes:

    IF   vehicle_speed > 60
    THEN create_violation, notify_officer, suggest_reroute

Rules are persisted to Postgres (``core.automation_rule``) with an in-memory
fallback so the composer works offline / in the mock build. Every evaluation is
appended to an execution log (``core.automation_execution``) that the UI renders
as an audit trail. The engine is transparent (a single field/operator/value
comparison + a named action list) — no black box.

    GET    /api/workflows/catalog          fields / operators / actions for the UI
    GET    /api/workflows/rules            list rules
    POST   /api/workflows/rules            create a rule
    PUT    /api/workflows/rules/{id}       update a rule
    DELETE /api/workflows/rules/{id}       delete a rule
    POST   /api/workflows/evaluate         run an event through all enabled rules (logs)
    GET    /api/workflows/executions       execution log (audit trail)
"""
from __future__ import annotations

import os

import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from ..logging import get_logger
from ..metrics import REQUESTS
from ..state import GatewayState, get_state

log = get_logger("gateway.workflows")

router = APIRouter(prefix="/api/workflows", tags=["workflows"])

# --- Authoring catalog (drives the UI dropdowns; keeps rules well-formed) -----
FIELDS = [
    {"key": "vehicle_speed", "label": "Vehicle speed", "unit": "km/h", "type": "number"},
    {"key": "speed_limit", "label": "Speed limit", "unit": "km/h", "type": "number"},
    {"key": "gate_queue_len", "label": "Gate queue length", "unit": "vehicles", "type": "number"},
    {"key": "congestion_p", "label": "Congestion probability", "unit": "0..1", "type": "number"},
    {"key": "dwell_min", "label": "Vehicle dwell", "unit": "min", "type": "number"},
    {"key": "vehicle_class", "label": "Vehicle class", "unit": "", "type": "string"},
    {"key": "weather", "label": "Weather", "unit": "", "type": "string"},
]
OPERATORS = [">", ">=", "<", "<=", "==", "!="]
ACTIONS = [
    {"key": "create_violation", "label": "Create violation case"},
    {"key": "notify_officer", "label": "Notify officer"},
    {"key": "suggest_reroute", "label": "Suggest reroute"},
    {"key": "raise_alert", "label": "Raise operator alert"},
    {"key": "issue_challan", "label": "Issue e-Challan"},
]
_FIELD_KEYS = {f["key"] for f in FIELDS}
_ACTION_KEYS = {a["key"] for a in ACTIONS}
_EXEC_CAP = 500

# --- In-memory store (fallback + fast cache) ----------------------------------
_RULES: Dict[str, dict] = {}
_EXECUTIONS: List[dict] = []
_loaded = False
_seeded = False


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _new_id() -> str:
    return f"rule-{int(time.time() * 1000)}-{len(_RULES)}"


def _validate_rule(body: dict) -> dict:
    name = str(body.get("name", "")).strip()
    field = body.get("field")
    op = body.get("op")
    value = body.get("value")
    actions = body.get("actions") or []
    if not name:
        raise HTTPException(422, detail={"error": "name_required"})
    if field not in _FIELD_KEYS:
        raise HTTPException(422, detail={"error": "unknown_field", "field": field})
    if op not in OPERATORS:
        raise HTTPException(422, detail={"error": "unknown_operator", "op": op})
    if not isinstance(actions, list) or not actions:
        raise HTTPException(422, detail={"error": "at_least_one_action_required"})
    bad = [a for a in actions if a not in _ACTION_KEYS]
    if bad:
        raise HTTPException(422, detail={"error": "unknown_actions", "actions": bad})
    return {"name": name, "field": field, "op": op, "value": value, "actions": actions,
            "enabled": bool(body.get("enabled", True))}


def _coerce(value: Any) -> Any:
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


def _cmp(lhs: Any, op: str, rhs: Any) -> bool:
    l, r = _coerce(lhs), _coerce(rhs)
    # If either side isn't numeric, only equality operators are meaningful.
    if not (isinstance(l, (int, float)) and isinstance(r, (int, float))):
        l, r = str(lhs), str(rhs)
        if op == "==":
            return l == r
        if op == "!=":
            return l != r
        return False
    try:
        return {">": l > r, ">=": l >= r, "<": l < r, "<=": l <= r,
                "==": l == r, "!=": l != r}[op]
    except KeyError:
        return False


# --- DB persistence (best-effort; falls back to memory) -----------------------
async def _ensure_tables(state: GatewayState) -> bool:
    if os.getenv("JNPA_RUNTIME_DDL", "0") != "1":
        # schema-v3: tables are pre-provisioned by infra/postgres/v3 migrations —
        # verify reachability instead of issuing DDL.
        from jnpa_shared.db import fetch_one
        try:
            row = await fetch_one("SELECT to_regclass('core.automation_rule') AS t",
                                  dsn=state.cfg.postgres_dsn)
            return bool(row and row.get("t"))
        except Exception:  # noqa: BLE001 — memory fallback, same as legacy behaviour
            return False
    from jnpa_shared.db import execute
    try:
        await execute(
            """
            CREATE TABLE IF NOT EXISTS core.automation_rule (
                id text PRIMARY KEY,
                name text NOT NULL,
                enabled boolean NOT NULL DEFAULT true,
                field text NOT NULL,
                op text NOT NULL,
                value text NOT NULL,
                actions jsonb NOT NULL DEFAULT '[]'::jsonb,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now()
            )
            """,
            {}, dsn=state.cfg.postgres_dsn,
        )
        await execute(
            """
            CREATE TABLE IF NOT EXISTS core.automation_execution (
                id bigserial PRIMARY KEY,
                ts timestamptz NOT NULL DEFAULT now(),
                event jsonb NOT NULL,
                results jsonb NOT NULL,
                matched_count int NOT NULL DEFAULT 0
            )
            """,
            {}, dsn=state.cfg.postgres_dsn,
        )
        return True
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        log.debug("workflows_ensure_tables_failed", error=str(exc))
        return False


async def _load_rules(state: GatewayState) -> None:
    """Hydrate the in-memory store from the DB once per process (best-effort)."""
    global _loaded
    if _loaded:
        return
    from jnpa_shared.db import fetch_all
    try:
        if await _ensure_tables(state):
            rows = await fetch_all(
                "SELECT id, name, enabled, field, op, value, actions, "
                "created_at, updated_at FROM core.automation_rule ORDER BY created_at",
                {}, dsn=state.cfg.postgres_dsn,
            )
            for r in rows:
                d = dict(r)
                for k in ("created_at", "updated_at"):
                    if isinstance(d.get(k), datetime):
                        d[k] = d[k].isoformat()
                if isinstance(d.get("actions"), str):
                    d["actions"] = json.loads(d["actions"])
                _RULES[d["id"]] = d
    except Exception as exc:  # pragma: no cover
        log.debug("workflows_load_failed", error=str(exc))
    _loaded = True
    _seed_defaults()


def _seed_defaults() -> None:
    """Seed two illustrative rules the first time the store is empty."""
    global _seeded
    if _seeded or _RULES:
        return
    _seeded = True
    for r in (
        {"name": "Over-speed → violation", "field": "vehicle_speed", "op": ">", "value": "60",
         "actions": ["create_violation", "notify_officer"]},
        {"name": "Heavy congestion → reroute", "field": "congestion_p", "op": ">=", "value": "0.7",
         "actions": ["suggest_reroute", "raise_alert"]},
    ):
        rid = _new_id()
        _RULES[rid] = {"id": rid, "enabled": True, "created_at": _now_iso(),
                       "updated_at": _now_iso(), **r}


async def _db_write_rule(state: GatewayState, rule: dict) -> None:
    from jnpa_shared.db import execute
    try:
        await execute(
            """
            INSERT INTO core.automation_rule
                (id, name, enabled, field, op, value, actions, created_at, updated_at)
            VALUES (:id, :name, :enabled, :field, :op, :value, CAST(:actions AS jsonb),
                    :created_at, :updated_at)
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name, enabled = EXCLUDED.enabled, field = EXCLUDED.field,
                op = EXCLUDED.op, value = EXCLUDED.value, actions = EXCLUDED.actions,
                updated_at = EXCLUDED.updated_at
            """,
            {**rule, "actions": json.dumps(rule["actions"]),
             "created_at": datetime.fromisoformat(rule["created_at"]),
             "updated_at": datetime.fromisoformat(rule["updated_at"])},
            dsn=state.cfg.postgres_dsn,
        )
    except Exception as exc:  # pragma: no cover
        log.debug("workflows_db_write_failed", error=str(exc))


async def _db_delete_rule(state: GatewayState, rule_id: str) -> None:
    from jnpa_shared.db import execute
    try:
        await execute("DELETE FROM core.automation_rule WHERE id = :id",
                      {"id": rule_id}, dsn=state.cfg.postgres_dsn)
    except Exception as exc:  # pragma: no cover
        log.debug("workflows_db_delete_failed", error=str(exc))


async def _db_write_execution(state: GatewayState, rec: dict) -> None:
    from jnpa_shared.db import execute
    try:
        await execute(
            "INSERT INTO core.automation_execution (ts, event, results, matched_count) "
            "VALUES (:ts, CAST(:event AS jsonb), CAST(:results AS jsonb), :matched)",
            {"ts": datetime.fromisoformat(rec["ts"]), "event": json.dumps(rec["event"]),
             "results": json.dumps(rec["results"]), "matched": rec["matched_count"]},
            dsn=state.cfg.postgres_dsn,
        )
    except Exception as exc:  # pragma: no cover
        log.debug("workflows_db_exec_write_failed", error=str(exc))


# --- Routes -------------------------------------------------------------------
@router.get("/catalog")
async def catalog() -> dict:
    REQUESTS.labels("workflows", "ok").inc()
    return {"fields": FIELDS, "operators": OPERATORS, "actions": ACTIONS}


@router.get("/rules")
async def list_rules(state: GatewayState = Depends(get_state)) -> dict:
    await _load_rules(state)
    REQUESTS.labels("workflows", "ok").inc()
    return {"rules": sorted(_RULES.values(), key=lambda r: r["created_at"]),
            "count": len(_RULES)}


@router.post("/rules")
async def create_rule(body: Dict[str, Any] = Body(...),
                      state: GatewayState = Depends(get_state)) -> dict:
    await _load_rules(state)
    fields = _validate_rule(body)
    rid = _new_id()
    rule = {"id": rid, "created_at": _now_iso(), "updated_at": _now_iso(), **fields,
            "value": str(fields["value"])}
    _RULES[rid] = rule
    await _db_write_rule(state, rule)
    REQUESTS.labels("workflows", "ok").inc()
    return {"rule": rule}


@router.put("/rules/{rule_id}")
async def update_rule(rule_id: str, body: Dict[str, Any] = Body(...),
                      state: GatewayState = Depends(get_state)) -> dict:
    await _load_rules(state)
    existing = _RULES.get(rule_id)
    if not existing:
        raise HTTPException(404, detail={"error": "rule_not_found"})
    fields = _validate_rule({**existing, **body})
    rule = {**existing, **fields, "value": str(fields["value"]), "updated_at": _now_iso()}
    _RULES[rule_id] = rule
    await _db_write_rule(state, rule)
    REQUESTS.labels("workflows", "ok").inc()
    return {"rule": rule}


@router.delete("/rules/{rule_id}")
async def delete_rule(rule_id: str, state: GatewayState = Depends(get_state)) -> dict:
    await _load_rules(state)
    if _RULES.pop(rule_id, None) is None:
        raise HTTPException(404, detail={"error": "rule_not_found"})
    await _db_delete_rule(state, rule_id)
    REQUESTS.labels("workflows", "ok").inc()
    return {"deleted": rule_id}


@router.post("/evaluate")
async def evaluate(body: Dict[str, Any] = Body(...),
                   state: GatewayState = Depends(get_state)) -> dict:
    """Run one event through all ENABLED rules and log the outcome (audit)."""
    await _load_rules(state)
    event = body.get("event") or body  # accept {event:{...}} or a bare event
    results: List[dict] = []
    for rule in _RULES.values():
        if not rule.get("enabled", True):
            continue
        present = rule["field"] in event
        matched = present and _cmp(event.get(rule["field"]), rule["op"], rule["value"])
        results.append({
            "rule_id": rule["id"], "name": rule["name"],
            "condition": f'{rule["field"]} {rule["op"]} {rule["value"]}',
            "field_present": present, "matched": bool(matched),
            "actions_fired": rule["actions"] if matched else [],
        })
    matched_count = sum(1 for r in results if r["matched"])
    rec = {"ts": _now_iso(), "event": event, "results": results,
           "matched_count": matched_count}
    _EXECUTIONS.append(rec)
    del _EXECUTIONS[:-_EXEC_CAP]
    await _db_write_execution(state, rec)
    REQUESTS.labels("workflows", "ok").inc()
    return rec


@router.get("/executions")
async def executions(limit: int = Query(default=50, ge=1, le=_EXEC_CAP),
                     state: GatewayState = Depends(get_state)) -> dict:
    """Execution log (audit trail). Reads the durable DB log when available."""
    from jnpa_shared.db import fetch_all
    rows: List[dict] = []
    try:
        if await _ensure_tables(state):
            db_rows = await fetch_all(
                "SELECT ts, event, results, matched_count FROM core.automation_execution "
                "ORDER BY ts DESC LIMIT :lim", {"lim": limit}, dsn=state.cfg.postgres_dsn,
            )
            for r in db_rows:
                d = dict(r)
                if isinstance(d.get("ts"), datetime):
                    d["ts"] = d["ts"].isoformat()
                for k in ("event", "results"):
                    if isinstance(d.get(k), str):
                        d[k] = json.loads(d[k])
                rows.append(d)
    except Exception as exc:  # pragma: no cover
        log.debug("workflows_exec_read_failed", error=str(exc))
    if not rows:  # DB empty/unavailable -> in-memory log (newest first)
        rows = list(reversed(_EXECUTIONS[-limit:]))
    REQUESTS.labels("workflows", "ok").inc()
    return {"executions": rows, "count": len(rows)}
