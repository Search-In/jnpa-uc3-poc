"""/api/reports/police — traffic-police incident reports.

    GET /api/reports/police                     -> JSON list of police-relevant
                                                   alerts (the table the dashboard
                                                   renders), filterable.
    GET /api/reports/police?format=pdf          -> a one-page-per-incident PDF
                                                   compiled server-side with
                                                   Playwright (Chromium),
                                                   embedding the photographic
                                                   evidence, plate + RC info, and
                                                   a pre-filled e-Challan payload.

Police-relevant alert kinds (spec):
    WRONG_WAY, ILLEGAL_PARKING, OVERSPEEDING, ROUTE_DEVIATION

The PDF is rendered from an HTML template via Playwright's ``page.pdf()``. If
Playwright / its Chromium browser is not available in the running image we fall
back to returning the same HTML (``Content-Type: text/html``) so the feature
degrades gracefully rather than 500-ing during a demo; the dashboard's
"Export PDF" button still produces a printable one-pager (Ctrl-P).
"""
from __future__ import annotations

import html
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, Response

from ..logging import get_logger
from ..metrics import REQUESTS
from ..state import GatewayState, get_state

log = get_logger("gateway.reports")

router = APIRouter(prefix="/api/reports", tags=["reports"])

# The kinds traffic police act on (the others are operational only).
POLICE_KINDS = ("WRONG_WAY", "ILLEGAL_PARKING", "OVERSPEEDING", "ROUTE_DEVIATION")

# Recommended action + e-Challan section per kind (Motor Vehicles Act references
# are indicative PoC values for the pre-filled challan payload).
_CHALLAN: Dict[str, Dict[str, Any]] = {
    "WRONG_WAY": {
        "action": "Issue e-Challan — driving against authorised flow",
        "section": "MVA s.184 (dangerous driving)",
        "fine_inr": 5000,
    },
    "ILLEGAL_PARKING": {
        "action": "Issue e-Challan — stopping/standing in a no-parking zone",
        "section": "MVA s.122/177 (obstruction)",
        "fine_inr": 1000,
    },
    "OVERSPEEDING": {
        "action": "Issue e-Challan — exceeding the corridor speed limit",
        "section": "MVA s.183 (over-speeding)",
        "fine_inr": 2000,
    },
    "ROUTE_DEVIATION": {
        "action": "Flag for inspection — deviation from the assigned corridor route",
        "section": "JNPA corridor SOP / MVA s.177",
        "fine_inr": 500,
    },
}


async def _police_alerts(
    state: GatewayState,
    *,
    kind: Optional[str],
    gate_id: Optional[str],
    severity: Optional[str],
    since: Optional[str],
    until: Optional[str],
    limit: int,
) -> List[dict]:
    from jnpa_shared.db import fetch_all

    clauses = ["kind = ANY(:kinds)"]
    params: Dict[str, Any] = {
        "kinds": list(POLICE_KINDS) if not kind else [kind],
        "limit": limit,
    }
    if gate_id:
        clauses.append("gate_id = :gate_id")
        params["gate_id"] = gate_id
    if severity:
        clauses.append("severity = :severity")
        params["severity"] = severity
    if since:
        clauses.append("ts >= :since")
        params["since"] = since
    if until:
        clauses.append("ts <= :until")
        params["until"] = until
    where = " AND ".join(clauses)
    sql = f"""
        SELECT id, ts, kind, severity, gate_id, plate, payload, ack
        FROM jnpa.alerts
        WHERE {where}
        ORDER BY ts DESC
        LIMIT :limit
    """
    try:
        rows = await fetch_all(sql, params, dsn=state.cfg.postgres_dsn)
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        log.debug("police_alerts_failed", error=str(exc))
        return []
    out = []
    for r in rows:
        d = dict(r)
        d["id"] = str(d["id"])
        if isinstance(d.get("ts"), datetime):
            d["ts"] = d["ts"].isoformat()
        out.append(d)
    return out


async def _enrich_rc(state: GatewayState, plates: List[str]) -> Dict[str, dict]:
    """Look up RC info for the plates on the report (owner/class/state)."""
    if not plates:
        return {}
    from jnpa_shared.db import fetch_all

    try:
        rows = await fetch_all(
            """
            SELECT plate, owner_name_masked, vehicle_class, state, rto_code,
                   fastag_status, blacklist_status
            FROM jnpa.vehicle_master
            WHERE plate = ANY(:plates)
            """,
            {"plates": plates},
            dsn=state.cfg.postgres_dsn,
        )
    except Exception as exc:  # pragma: no cover
        log.debug("rc_enrich_failed", error=str(exc))
        return {}
    return {r["plate"]: dict(r) for r in rows}


@router.get("/police")
async def police_report(
    format: str = Query(default="json", pattern="^(json|pdf|html)$"),
    kind: Optional[str] = Query(default=None),
    gate: Optional[str] = Query(default=None, alias="gate"),
    severity: Optional[str] = Query(default=None),
    since: Optional[str] = Query(default=None, description="ISO-8601 timestamp"),
    until: Optional[str] = Query(default=None, description="ISO-8601 timestamp"),
    limit: int = Query(default=50, ge=1, le=500),
    state: GatewayState = Depends(get_state),
):
    alerts = await _police_alerts(
        state, kind=kind, gate_id=gate, severity=severity,
        since=since, until=until, limit=limit,
    )
    plates = sorted({a["plate"] for a in alerts if a.get("plate")})
    rc = await _enrich_rc(state, plates)

    # Pre-fill the e-Challan payload per incident.
    incidents = []
    for a in alerts:
        challan = dict(_CHALLAN.get(a["kind"], {}))
        challan["plate"] = a.get("plate")
        challan["incident_id"] = a["id"]
        challan["issued_at"] = a.get("ts")
        # Evidence must be an object-store URL (MinIO/S3), never an inline base64
        # data-URL — drop any legacy data: value so reports only ever reference
        # durable, retained evidence.
        _evidence = (a.get("payload") or {}).get("evidence_url")
        if isinstance(_evidence, str) and _evidence.startswith("data:"):
            _evidence = None
        incidents.append({
            **a,
            "rc": rc.get(a.get("plate") or "", {}),
            "challan": challan,
            "evidence_url": _evidence,
        })

    REQUESTS.labels("reports", "ok").inc()

    if format == "json":
        return {"incidents": incidents, "count": len(incidents)}

    html_doc = _render_html(incidents)
    if format == "html":
        return Response(content=html_doc, media_type="text/html")

    # format == pdf
    pdf = await _html_to_pdf(html_doc)
    if pdf is None:
        # Playwright unavailable — degrade to printable HTML (still one-per-page).
        log.warning("pdf_renderer_unavailable_degrading_to_html")
        return Response(
            content=html_doc, media_type="text/html",
            headers={"X-PDF-Fallback": "playwright-unavailable"},
        )
    return Response(
        content=pdf, media_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="jnpa-police-report.pdf"'},
    )


async def _html_to_pdf(html_doc: str) -> Optional[bytes]:
    """Render the HTML to a PDF with Playwright Chromium; None if unavailable."""
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:  # pragma: no cover - optional dep
        log.debug("playwright_import_failed", error=str(exc))
        return None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(args=["--no-sandbox"])
            page = await browser.new_page()
            await page.set_content(html_doc, wait_until="networkidle")
            pdf = await page.pdf(
                format="A4",
                print_background=True,
                margin={"top": "12mm", "bottom": "12mm", "left": "12mm", "right": "12mm"},
            )
            await browser.close()
            return pdf
    except Exception as exc:  # pragma: no cover - browser/runtime dependent
        log.warning("playwright_render_failed", error=str(exc))
        return None


def _esc(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def _severity_colour(sev: str) -> str:
    # Colour-blind-safe (Okabe-Ito) severity ramp.
    return {
        "REPORT_TO_POLICE": "#D55E00",
        "critical": "#D55E00",
        "warning": "#E69F00",
        "info": "#0072B2",
    }.get(sev, "#0072B2")


def _render_html(incidents: List[dict]) -> str:
    """One printable A4 page per incident (page-break between)."""
    pages = []
    for idx, inc in enumerate(incidents):
        rc = inc.get("rc") or {}
        challan = inc.get("challan") or {}
        evidence = inc.get("evidence_url")
        ev_html = (
            f'<img class="evidence" src="{_esc(evidence)}" '
            f'alt="evidence" onerror="this.style.display=\'none\'"/>'
            if evidence else
            '<div class="evidence noimg">No photographic evidence on file</div>'
        )
        sev = _esc(inc.get("severity"))
        pages.append(f"""
        <section class="incident" style="page-break-after: {'always' if idx < len(incidents) - 1 else 'auto'};">
          <header>
            <div>
              <h1>JNPA Traffic-Police Incident Report</h1>
              <div class="sub">NH-348 Corridor · Use Case III — Traffic Monitoring</div>
            </div>
            <div class="badge" style="background:{_severity_colour(inc.get('severity',''))}">{sev}</div>
          </header>
          <table class="kv">
            <tr><th>Incident ID</th><td>{_esc(inc.get('id'))}</td>
                <th>Kind</th><td>{_esc(inc.get('kind'))}</td></tr>
            <tr><th>Timestamp (UTC)</th><td>{_esc(inc.get('ts'))}</td>
                <th>Gate</th><td>{_esc(inc.get('gate_id') or '—')}</td></tr>
            <tr><th>Plate</th><td class="plate">{_esc(inc.get('plate') or '—')}</td>
                <th>Vehicle Class</th><td>{_esc(rc.get('vehicle_class') or '—')}</td></tr>
            <tr><th>Owner (masked)</th><td>{_esc(rc.get('owner_name_masked') or '—')}</td>
                <th>RTO / State</th><td>{_esc(rc.get('rto_code') or '—')} / {_esc(rc.get('state') or '—')}</td></tr>
            <tr><th>FASTag</th><td>{_esc(rc.get('fastag_status') or '—')}</td>
                <th>Blacklist</th><td>{_esc(rc.get('blacklist_status') or 'CLEAR')}</td></tr>
          </table>
          {ev_html}
          <div class="challan">
            <h2>Recommended action — e-Challan (pre-filled)</h2>
            <table class="kv">
              <tr><th>Action</th><td>{_esc(challan.get('action') or '—')}</td></tr>
              <tr><th>MVA Section</th><td>{_esc(challan.get('section') or '—')}</td>
                  <th>Fine (₹)</th><td>{_esc(challan.get('fine_inr') or '—')}</td></tr>
            </table>
            <pre class="payload">{_esc(json.dumps(challan, indent=2))}</pre>
          </div>
          <footer>Generated by the JNPA UC-III control room · evidence retained in MinIO</footer>
        </section>
        """)
    body = "\n".join(pages) or '<section class="incident"><h1>No incidents match the filter.</h1></section>'
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>JNPA Police Report</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: system-ui, -apple-system, "Segoe UI", sans-serif; color:#111; margin:0; }}
  .incident {{ padding: 8mm; }}
  header {{ display:flex; justify-content:space-between; align-items:flex-start;
            border-bottom:2px solid #111; padding-bottom:6px; margin-bottom:12px; }}
  h1 {{ font-size: 18px; margin:0; }}
  .sub {{ color:#555; font-size:12px; }}
  .badge {{ color:#fff; font-weight:700; padding:4px 10px; border-radius:4px; font-size:12px; }}
  table.kv {{ width:100%; border-collapse:collapse; margin:8px 0; font-size:12px; }}
  table.kv th {{ text-align:left; color:#555; font-weight:600; width:18%; padding:4px 6px;
                 vertical-align:top; }}
  table.kv td {{ padding:4px 6px; border-bottom:1px solid #eee; }}
  .plate {{ font-family: ui-monospace, monospace; font-weight:700; letter-spacing:1px; }}
  .evidence {{ display:block; max-width:100%; max-height:90mm; margin:10px 0;
               border:1px solid #ccc; border-radius:4px; }}
  .evidence.noimg {{ color:#888; font-style:italic; border:1px dashed #ccc; padding:24px;
                     text-align:center; }}
  .challan h2 {{ font-size:13px; margin:14px 0 4px; }}
  .payload {{ background:#f6f6f6; border:1px solid #e0e0e0; border-radius:4px; padding:8px;
              font-size:11px; white-space:pre-wrap; }}
  footer {{ margin-top:14px; color:#888; font-size:10px; border-top:1px solid #eee;
            padding-top:6px; }}
</style></head>
<body>{body}</body></html>"""
