"""FastAPI app: JNPA Simulated Port-Data API v2.0 — contract-faithful sim.

The same surface as the live service:

    GET  /                      service description (no auth)
    GET  /v2/health             liveness (no auth)
    POST /v2/auth/token         client key -> 1-hour bearer
    GET  /v2/groups             data-group catalogue
    GET  /v2/groups/{g}/records indexed records / report JSON
    GET  /v2/files/{fileRef}    file download (ETag / If-None-Match)

plus one test-only helper OUTSIDE the live surface:

    POST /admin/force-429       make the next N data requests answer 429

Faithful-defect emulation (cfg.faithful, default on) reproduces the live
service's catalogued quirks — sequential record ids under a base64 coat,
cursor == last item's fileRef, the 5-field report envelope, no requestId
anywhere, RateLimit-Remaining omitted on errors and 304s, the ~250 ms
slowdown on a bad client key, and 429 without Retry-After. See
docs/JNPA_API_DEFECTS.md for the catalogue; the sim is the executable half
of it.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from jnpa_shared.logging import configure_logging, get_logger

from .config import SimConfig
from . import seed as seed_mod
from .seed import (
    COVERAGE_FROM,
    COVERAGE_TO,
    GROUP_FOLDERS,
    GROUP_NAMES,
    IST,
    NLP_MARINE_MESSAGE_TYPES,
    REPORT_GROUPS,
    STATIC_GROUPS,
    TYPE_FILTER_VOCAB,
    SimIndex,
    build_index,
    matches_type_filter,
    synthetic_report_set,
)

cfg = SimConfig.from_env()
configure_logging(cfg.log_level)
log = get_logger("jnpa_portdata_sim")


def derive_client_key(email: str) -> str:
    """The published keygen algorithm (KEY_GENERATION.md)."""
    digest = hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()
    return base64.b64encode(digest[:20].encode("ascii")).decode("ascii")


def derive_client_id(email: str) -> str:
    return "cli_" + hashlib.sha256(
        email.strip().lower().encode("utf-8")).hexdigest()[:12]


class SimState:
    def __init__(self, config: SimConfig) -> None:
        self.cfg = config
        self.index: SimIndex = build_index(config.data_dir)
        self.started_monotonic = time.monotonic()
        # accepted key -> (client_id, organisation)
        self.keys: Dict[str, Tuple[str, str]] = {}
        for email in config.client_emails:
            self.keys[derive_client_key(email)] = (
                derive_client_id(email), "SIM-ORG")
        for key in config.extra_keys:
            self.keys[key] = ("cli_extrakey0000", "SIM-ORG")
        # token -> (client_id, expiry_monotonic)
        self.tokens: Dict[str, Tuple[str, float]] = {}
        self.force_429_remaining = config.force_429
        # sliding-window rate ledger (600/min like the captures imply — D4)
        self.rate_window: List[float] = []
        self.rate_limit = 600

    def now(self) -> datetime:
        return datetime.now(IST)

    def uptime_s(self) -> int:
        return int(time.monotonic() - self.started_monotonic)

    def rate_remaining(self) -> int:
        cutoff = time.monotonic() - 60.0
        self.rate_window = [t for t in self.rate_window if t >= cutoff]
        return max(0, self.rate_limit - len(self.rate_window))

    def count_request(self) -> int:
        self.rate_window.append(time.monotonic())
        return self.rate_remaining()


state = SimState(cfg)
app = FastAPI(title="JNPA Port-Data API sim", version="2.0-sim")


def _err(status: int, code: str, message: str, **extra) -> JSONResponse:
    # Faithful: no requestId (defect D3), no RateLimit header on errors (D5).
    body = {"error": code, "message": message}
    body.update(extra)
    return JSONResponse(status_code=status, content=body)


def _bearer_client(request: Request) -> Tuple[Optional[str], Optional[JSONResponse]]:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None, _err(401, "unauthorized", "Bearer token required")
    token = header[len("Bearer "):].strip()
    entry = state.tokens.get(token)
    if entry is None:
        return None, _err(401, "unauthorized", "Token is not valid")
    client_id, expiry = entry
    if time.monotonic() >= expiry:
        return None, _err(401, "unauthorized", "Token is not valid",
                          reason="expired")
    return client_id, None


def _check_429() -> Optional[JSONResponse]:
    if state.force_429_remaining > 0:
        state.force_429_remaining -= 1
        # Faithful: no Retry-After, no RateLimit-* headers (defects D5/D6).
        return _err(429, "rate_limited",
                    "The per-minute request allowance has been exhausted")
    return None


def _parse_instant(value: str, *, end_of_day: bool) -> Optional[datetime]:
    text = value.strip()
    try:
        if len(text) == 10:  # bare date
            day = datetime.fromisoformat(text)
            if end_of_day:
                day = day + timedelta(hours=23, minutes=59, seconds=59,
                                      microseconds=999999)
            return day.replace(tzinfo=IST)
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=IST)
        return parsed
    except ValueError:
        return None


@app.get("/")
async def service_description():
    return {
        "service": "JNPA Digital Twin — Simulated Port Data API",
        "version": "2.0",
        "timeZone": "Asia/Kolkata",
        "howToUse": [
            "POST /v2/auth/token with your clientKey to obtain a bearer token",
            "GET /v2/groups to list the data groups",
            "GET /v2/groups/{group}/records for indexed JSON with file references",
            "GET /v2/files/{fileRef} to download the referenced file",
        ],
        "notes": ("Records are returned newest first. Use since/until, "
                  "from/to or date to bound a request, and order=asc to walk "
                  "forward through history."),
        "endpoints": {
            "token": "/v2/auth/token",
            "groups": "/v2/groups",
            "records": "/v2/groups/{group}/records",
            "files": "/v2/files/{fileRef}",
            "health": "/v2/health",
        },
    }


@app.get("/v2/health")
async def health():
    return {"status": "ok", "uptimeSeconds": state.uptime_s()}


@app.post("/v2/auth/token")
async def token(request: Request):
    try:
        body = await request.json()
    except Exception:
        return _err(400, "bad_request", "The request body is not valid JSON")
    if not isinstance(body, dict):
        return _err(400, "bad_request", "The request body is not valid JSON")
    client_key = str(body.get("clientKey", "") or "")
    entry = state.keys.get(client_key)
    if entry is None:
        # The deliberate slow-lane on the guessing path.
        await asyncio.sleep(state.cfg.bad_key_delay_s)
        return _err(401, "unauthorized", "Client key not recognised")
    client_id, organisation = entry
    issued = secrets.token_urlsafe(24)
    ttl = state.cfg.token_ttl_s
    state.tokens[issued] = (client_id, time.monotonic() + ttl)
    now = state.now()
    return {
        "accessToken": issued,
        "tokenType": "Bearer",
        "expiresIn": int(ttl),
        "expiresAt": (now + timedelta(seconds=ttl)).isoformat(),
        # Faithful D0b: the captured live token carries an undocumented
        # third scope.
        "scopes": ["groups:read", "files:read", "admin:read"],
        "client": {"id": client_id, "organisation": organisation},
    }


@app.get("/v2/groups")
async def groups(request: Request):
    _, auth_error = _bearer_client(request)
    if auth_error:
        return auth_error
    denied = _check_429()
    if denied:
        return denied
    remaining = state.count_request()
    now = state.now()
    catalogue = []
    for group in GROUP_FOLDERS:
        records = state.index.group_records(group)
        visible = [r for r in records if r.published_at <= now]
        entry: Dict = {
            "group": group,
            "name": GROUP_NAMES[group],
            "description": f"{GROUP_NAMES[group]} (simulated).",
            "delivery": ("static" if group in STATIC_GROUPS
                         else "report" if group in REPORT_GROUPS
                         else "indexed"),
            "links": {"records": f"/v2/groups/{group}/records"},
            "records": len(visible),
        }
        # Faithful D11: the catalogue schema is non-uniform.
        if entry["delivery"] == "indexed" and visible:
            entry["coverage"] = {
                "from": min(r.published_at for r in visible).isoformat(),
                "to": max(r.published_at for r in visible).isoformat(),
            }
        if group == "nlp-marine":
            entry["messageTypes"] = NLP_MARINE_MESSAGE_TYPES
        if group == "bathymetry":
            entry["note"] = "Static reference carried over from the sample set."
        catalogue.append(entry)
    return JSONResponse(
        content={"groups": catalogue},
        headers={"RateLimit-Remaining": str(remaining)})


@app.get("/v2/groups/{group}/records")
async def records(group: str, request: Request):
    _, auth_error = _bearer_client(request)
    if auth_error:
        return auth_error
    if group not in GROUP_FOLDERS:
        return _err(404, "unknown_group", f"No group '{group}'",
                    availableGroups=list(GROUP_FOLDERS))
    denied = _check_429()
    if denied:
        return denied

    query = request.query_params
    now = state.now()
    as_of = now.isoformat()

    # ---- report groups: JSON items, 5-field envelope, NOT paginated ----
    # The live endpoint returns the FULL set in one call, each item
    # self-describing with its own reportDate (+ terminal for berthing); it
    # applies no date/terminal request filter. We mirror that: one answer,
    # every item.
    if group in REPORT_GROUPS:
        remaining = state.count_request()
        items: List[Dict] = []
        if state.cfg.report_items == "synthetic":
            items = synthetic_report_set(group, now)
        body = {
            "group": group,
            "delivery": "report",
            "asOf": as_of,
            "count": len(items),
            "items": items,
        }
        return JSONResponse(content=body,
                            headers={"RateLimit-Remaining": str(remaining)})

    # ---------------- indexed (and static) groups ----------------
    limit_raw = query.get("limit", "50")
    try:
        limit = int(limit_raw)
    except ValueError:
        return _err(400, "bad_parameter", f"limit '{limit_raw}' is not a number")
    if not 1 <= limit <= 500:
        return _err(400, "bad_parameter",
                    "limit must be between 1 and 500")
    order = query.get("order", "desc")
    if order not in ("asc", "desc"):
        return _err(400, "bad_parameter", "order must be 'asc' or 'desc'")
    type_param = query.get("type")
    if type_param and type_param not in TYPE_FILTER_VOCAB:
        return _err(400, "bad_parameter",
                    f"type must be one of {', '.join(TYPE_FILTER_VOCAB)}")

    bounds: Dict[str, Optional[datetime]] = {}
    for name, end_of_day in (("from", False), ("to", True),
                             ("since", False), ("until", True)):
        raw = query.get(name)
        if raw is None:
            continue
        parsed = _parse_instant(raw, end_of_day=end_of_day)
        if parsed is None:
            # A raw '+05:30' arrives as ' 05:30' (space) and lands here —
            # the server-side face of defect D22.
            return _err(400, "bad_parameter",
                        f"{name} '{raw}' is not a date or instant")
        bounds[name] = parsed

    visible = [r for r in state.index.group_records(group)
               if r.published_at <= now]
    if "from" in bounds:
        visible = [r for r in visible if r.published_at >= bounds["from"]]
    if "to" in bounds:
        visible = [r for r in visible if r.published_at <= bounds["to"]]
    if "since" in bounds:  # EXCLUSIVE lower bound
        visible = [r for r in visible if r.published_at > bounds["since"]]
    if "until" in bounds:
        visible = [r for r in visible if r.published_at <= bounds["until"]]
    if type_param:
        visible = [r for r in visible if matches_type_filter(r, type_param)]

    # Sort on publishedAt ONLY — no tie-break key, faithfully (defect D13b).
    visible.sort(key=lambda r: r.published_at, reverse=(order == "desc"))
    matched = len(visible)

    cursor = query.get("cursor")
    start = 0
    if cursor:
        position = next((i for i, r in enumerate(visible)
                         if r.file_ref == cursor), None)
        if position is None:
            return _err(400, "bad_cursor",
                        "The cursor was not issued by this interface")
        start = position + 1
    page = visible[start:start + limit]
    has_more = (start + limit) < matched
    # Faithful D13: the cursor shares the fileRef namespace — it IS the last
    # item's fileRef.
    next_cursor = page[-1].file_ref if (has_more and page) else None

    remaining = state.count_request()
    body = {
        "asOf": as_of,
        "group": group,
        "delivery": "static" if group in STATIC_GROUPS else "indexed",
        "order": order,
        "count": len(page),
        "matched": matched,
        "hasMore": has_more,
        "nextCursor": next_cursor,
        "items": [r.as_item() for r in page],
    }
    return JSONResponse(content=body,
                        headers={"RateLimit-Remaining": str(remaining)})


@app.get("/v2/files/{file_ref}")
async def files(file_ref: str, request: Request):
    _, auth_error = _bearer_client(request)
    if auth_error:
        return auth_error
    denied = _check_429()
    if denied:
        return denied
    record = state.index.by_file_ref.get(file_ref)
    if record is None or record.published_at > state.now():
        return _err(404, "invalid_reference",
                    "That file reference is not valid or has been altered")
    etag = f'"{record.sha256}"'
    if_none_match = request.headers.get("If-None-Match", "").strip()
    state.count_request()
    if if_none_match and if_none_match.strip('"') == record.sha256:
        # Faithful: 304 carries the ETag but NO RateLimit-Remaining and NO
        # Content-Disposition (defects D5/D27).
        return Response(status_code=304, headers={"ETag": etag})
    path = state.index.data_dir / record.rel_path
    try:
        content = path.read_bytes()
    except OSError:
        return _err(404, "not_found", "No file for that reference")
    return Response(
        content=content,
        media_type=record.media_type,
        headers={
            "ETag": etag,
            "Content-Disposition": f'attachment; filename="{record.filename}"',
            "RateLimit-Remaining": str(state.rate_remaining()),
        },
    )


# --------------------------- test-only helpers ---------------------------
@app.post("/admin/force-429")
async def force_429(request: Request):
    """NOT part of the live surface. Arm N forced 429 answers."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    count = int(body.get("count", 1)) if isinstance(body, dict) else 1
    state.force_429_remaining = max(0, count)
    return {"armed": state.force_429_remaining}


def main() -> None:  # pragma: no cover
    import uvicorn

    log.info("jnpa_portdata_sim_start", data_dir=cfg.data_dir,
             groups=len(GROUP_FOLDERS), records=state.index.total,
             faithful=cfg.faithful)
    uvicorn.run(app, host=cfg.host, port=cfg.port)


if __name__ == "__main__":  # pragma: no cover
    main()
