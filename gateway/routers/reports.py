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
from datetime import datetime, timezone
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
    incident_id: Optional[str] = None,
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
    # Single-incident export ("Download this report"): pin to one alert so the
    # PDF holds exactly the clicked incident, not every incident of its kind.
    # Cast id::text so this works whether the column is uuid or text.
    if incident_id:
        clauses.append("id::text = :incident_id")
        params["incident_id"] = incident_id
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
    id: Optional[str] = Query(default=None, description="single incident id"),
    kind: Optional[str] = Query(default=None),
    gate: Optional[str] = Query(default=None, alias="gate"),
    severity: Optional[str] = Query(default=None),
    since: Optional[str] = Query(default=None, description="ISO-8601 timestamp"),
    until: Optional[str] = Query(default=None, description="ISO-8601 timestamp"),
    limit: int = Query(default=50, ge=1, le=500),
    state: GatewayState = Depends(get_state),
):
    alerts = await _police_alerts(
        state, incident_id=id, kind=kind, gate_id=gate, severity=severity,
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

    if format == "pdf":
        # Playwright renders inside the gateway container and can't resolve the
        # relative /api/evidence path, so point it at the gateway itself (same
        # container) — the route is public, so the image embeds without a token.
        for inc in incidents:
            ev = inc.get("evidence_url")
            if isinstance(ev, str) and ev.startswith("/api/evidence/"):
                inc["evidence_url"] = f"http://localhost:8000{ev}"

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


# Readable incident-kind labels (mirrors the dashboard's alertKind i18n).
_KIND_LABELS: Dict[str, str] = {
    "WRONG_WAY": "Wrong-way driving",
    "ILLEGAL_PARKING": "Illegal parking",
    "OVERSPEEDING": "Over-speeding",
    "ROUTE_DEVIATION": "Route deviation",
}


def _kind_label(kind: Optional[str]) -> str:
    if not kind:
        return "—"
    return _KIND_LABELS.get(kind, kind.replace("_", " ").title())


def _fmt_inr(value: Any) -> str:
    """Indian-grouped rupee amount, e.g. 5000 -> ₹5,000."""
    try:
        return f"₹{int(value):,}"
    except (TypeError, ValueError):
        return "—"


def _render_html(incidents: List[dict]) -> str:
    """One printable A4 page per incident (page-break between)."""
    generated = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
    total = len(incidents)
    pages = []
    for idx, inc in enumerate(incidents):
        rc = inc.get("rc") or {}
        challan = inc.get("challan") or {}
        evidence = inc.get("evidence_url")
        ev_html = (
            f'<figure class="evidence-wrap">'
            f'<img class="evidence" src="{_esc(evidence)}" '
            f'alt="evidence" onerror="this.parentNode.style.display=\'none\'"/>'
            f'<figcaption>Photographic evidence · retained in MinIO object store</figcaption>'
            f'</figure>'
            if evidence else
            '<div class="evidence noimg">No photographic evidence on file</div>'
        )
        sev = _esc(inc.get("severity"))
        kind = inc.get("kind") or ""
        pages.append(f"""
        <section class="incident" style="page-break-after: {'always' if idx < total - 1 else 'auto'};">
          <header>
            <div class="brand">
              <div class="emblem">JNPA</div>
              <div>
                <h1>Traffic-Police Incident Report</h1>
                <div class="sub">NH-348 Corridor · Use Case III — Traffic Monitoring &amp; Enforcement</div>
              </div>
            </div>
            <div class="badge" style="background:{_severity_colour(inc.get('severity',''))}">{sev}</div>
          </header>

          <div class="incident-title">
            <span class="kind">{_esc(_kind_label(kind))}</span>
            <span class="kind-code">{_esc(kind)}</span>
            <span class="ref">Page {idx + 1} of {total}</span>
          </div>

          <h2 class="section-h">Incident &amp; vehicle details</h2>
          <table class="kv">
            <tr><th>Incident ID</th><td>{_esc(inc.get('id'))}</td>
                <th>Timestamp (UTC)</th><td>{_esc(inc.get('ts'))}</td></tr>
            <tr><th>Gate</th><td>{_esc(inc.get('gate_id') or '—')}</td>
                <th>Plate</th><td class="plate">{_esc(inc.get('plate') or '—')}</td></tr>
            <tr><th>Vehicle class</th><td>{_esc(rc.get('vehicle_class') or '—')}</td>
                <th>Owner (masked)</th><td>{_esc(rc.get('owner_name_masked') or '—')}</td></tr>
            <tr><th>RTO / State</th><td>{_esc(rc.get('rto_code') or '—')} / {_esc(rc.get('state') or '—')}</td>
                <th>FASTag</th><td>{_esc(rc.get('fastag_status') or '—')}</td></tr>
            <tr><th>Blacklist</th><td>{_esc(rc.get('blacklist_status') or 'CLEAR')}</td>
                <th></th><td></td></tr>
          </table>

          {ev_html}

          <div class="challan">
            <h2 class="section-h">Recommended action — e-Challan <span class="prefilled">pre-filled</span></h2>
            <div class="challan-body">
              <div class="action">{_esc(challan.get('action') or '—')}</div>
              <div class="challan-grid">
                <div class="cell">
                  <div class="label">MVA Section</div>
                  <div class="value">{_esc(challan.get('section') or '—')}</div>
                </div>
                <div class="cell fine">
                  <div class="label">Fine payable</div>
                  <div class="value amount">{_fmt_inr(challan.get('fine_inr'))}</div>
                </div>
              </div>
            </div>
          </div>

          <footer>
            <span>Generated by the JNPA UC-III control room · {generated}</span>
            <span>Evidence retained in MinIO · This is a system-generated document.</span>
          </footer>
        </section>
        """)
    body = "\n".join(pages) or f"""
        <section class="incident">
          <header>
            <div class="brand">
              <div class="emblem">JNPA</div>
              <div>
                <h1>Traffic-Police Incident Report</h1>
                <div class="sub">NH-348 Corridor · Use Case III — Traffic Monitoring &amp; Enforcement</div>
              </div>
            </div>
          </header>
          <div class="empty">
            <div class="empty-title">No incidents to report</div>
            <div class="empty-sub">No police-relevant incidents match the selected filters.</div>
          </div>
          <footer>
            <span>Generated by the JNPA UC-III control room · {generated}</span>
            <span>This is a system-generated document.</span>
          </footer>
        </section>
    """
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>JNPA Police Report</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
          color:#1a1a1a; margin:0; -webkit-print-color-adjust:exact; print-color-adjust:exact; }}
  .incident {{ padding: 10mm; position:relative; }}
  .incident::before {{ content:""; position:absolute; top:0; left:0; right:0; height:5px;
                       background:linear-gradient(90deg,#0072B2,#56B4E9); }}
  header {{ display:flex; justify-content:space-between; align-items:flex-start;
            border-bottom:2px solid #0072B2; padding:14px 0 10px; margin-bottom:14px; }}
  .brand {{ display:flex; align-items:center; gap:12px; }}
  .emblem {{ width:42px; height:42px; border-radius:8px; background:#0072B2; color:#fff;
             font-weight:800; font-size:13px; letter-spacing:.5px;
             display:flex; align-items:center; justify-content:center; }}
  h1 {{ font-size: 18px; margin:0; letter-spacing:.2px; }}
  .sub {{ color:#666; font-size:11px; margin-top:2px; }}
  .badge {{ color:#fff; font-weight:700; padding:5px 12px; border-radius:999px; font-size:11px;
            letter-spacing:.4px; white-space:nowrap; }}
  .incident-title {{ display:flex; align-items:center; gap:10px; margin:0 0 12px; }}
  .incident-title .kind {{ font-size:16px; font-weight:700; }}
  .incident-title .kind-code {{ font-family: ui-monospace, monospace; font-size:10px; color:#888;
                                background:#f1f1f1; border-radius:4px; padding:2px 6px; }}
  .incident-title .ref {{ margin-left:auto; font-size:11px; color:#888; }}
  .section-h {{ font-size:12px; text-transform:uppercase; letter-spacing:.6px; color:#0072B2;
                margin:16px 0 6px; font-weight:700; }}
  table.kv {{ width:100%; border-collapse:collapse; margin:4px 0; font-size:12px;
              border:1px solid #e6e6e6; border-radius:6px; overflow:hidden; }}
  table.kv th {{ text-align:left; color:#666; font-weight:600; width:16%; padding:7px 10px;
                 vertical-align:top; background:#fafafa; }}
  table.kv td {{ padding:7px 10px; border-bottom:1px solid #eee; }}
  table.kv tr:last-child th, table.kv tr:last-child td {{ border-bottom:none; }}
  .plate {{ font-family: ui-monospace, monospace; font-weight:700; letter-spacing:1px; }}
  .evidence-wrap {{ margin:12px 0; }}
  .evidence {{ display:block; max-width:100%; max-height:88mm; margin:0;
               border:1px solid #ddd; border-radius:6px; }}
  .evidence-wrap figcaption {{ color:#888; font-size:10px; margin-top:4px; }}
  .evidence.noimg {{ color:#999; font-style:italic; border:1px dashed #ccc; padding:24px;
                     text-align:center; border-radius:6px; margin:12px 0; }}
  .challan {{ margin-top:8px; }}
  .prefilled {{ font-size:9px; font-weight:700; color:#E69F00; background:#fdf3e2;
                border-radius:999px; padding:2px 8px; letter-spacing:.3px; vertical-align:middle; }}
  .challan-body {{ border:1px solid #f0d9ad; border-left:4px solid #E69F00; background:#fffdf8;
                   border-radius:6px; padding:12px 14px; }}
  .challan-body .action {{ font-size:13px; font-weight:600; margin-bottom:10px; }}
  .challan-grid {{ display:flex; gap:14px; }}
  .challan-grid .cell {{ flex:1; }}
  .challan-grid .label {{ font-size:10px; text-transform:uppercase; letter-spacing:.5px; color:#888; }}
  .challan-grid .value {{ font-size:13px; font-weight:600; margin-top:2px; }}
  .challan-grid .amount {{ font-size:18px; color:#D55E00; }}
  .empty {{ padding:60px 40px; text-align:center; }}
  .empty-title {{ font-size:16px; font-weight:700; color:#333; }}
  .empty-sub {{ font-size:12px; color:#999; margin-top:6px; }}
  footer {{ display:flex; justify-content:space-between; margin-top:18px; color:#999; font-size:10px;
            border-top:1px solid #eee; padding-top:8px; }}
</style></head>
<body>{body}</body></html>"""
