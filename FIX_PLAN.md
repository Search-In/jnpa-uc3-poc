# FIX_PLAN.md

**Project:** JNPA UC-3 Port Traffic Digital Twin
**Date:** 2026-06-23
**Purpose:** Exact, ordered remediation for the gaps in [GAP_REPORT.md](GAP_REPORT.md). Code blocks are proposed diffs — review imports against the target file before applying.

Priority order: **P0 → P1 → P2**. Estimated total: **~38–52 engineer-hours** (excl. real third-party integrations).

---

## §1 (P1) — Fix Vahan RC field mismatch  ·  ~1 h

**Problem:** PWA Profile reads `insurance_upto / fitness_upto / maker / model`; backend `VahanRecord` emits `insurance_valid_to / fitness_valid_to` and has no maker/model.

**Cheapest correct fix:** update the PWA consumer to the canonical names (keep legacy aliases for safety). File: [`mobile-pwa/src/screens/Profile.tsx`](mobile-pwa/src/screens/Profile.tsx).

```tsx
// Insurance — prefer canonical backend field, keep legacy aliases
<Row
  k={t("profile.insuranceUpto")}
  v={rc.insurance_valid_to || rc.insurance_upto || rc.insurance_validity || t("common.noData")}
/>
// Fitness
<Row
  k={t("profile.fitnessUpto")}
  v={rc.fitness_valid_to || rc.fitness_upto || t("common.noData")}
/>
```

**Maker/Model:** `VahanRecord` has no such fields. Two options:
- **(a) Recommended, 0 backend change):** replace the Maker/Model row with `vehicle_class` (already present) or drop it.
- **(b)** add `maker`/`model` to `VahanRecord` (`shared/jnpa_shared/schemas.py:109`), the `vahan_sim` seed, and the `jnpa.vehicle_master` writeback — only if the data genuinely exists upstream.

```tsx
// Option (a): show class instead of unavailable maker/model
<Row k={t("profile.vehicleClass")} v={rc.vehicle_class || rc.vehicle_category || t("common.noData")} />
```

---

## §2 (P1) — Add missing gateway proxy routes  ·  ~3 h

### 2a. `GET /api/anpr/eval` → proxy `ai/anpr GET /eval`
Append to [`gateway/routers/anpr.py`](gateway/routers/anpr.py) (mirrors the existing `infer()` proxy pattern at line 136):

```python
@router.get("/eval")
async def eval_metrics(state: GatewayState = Depends(get_state)) -> dict:
    """Proxy ai/anpr's held-out OCR benchmark so the dashboard realism probe
    (web/src/data/live.ts:ocrEval) can render the accuracy panel."""
    cfg = state.cfg
    url = cfg.anpr_ai_url.rstrip("/") + "/eval"
    try:
        resp = await state.http.get(url, timeout=10.0)
        if resp.status_code == 200:
            REQUESTS.labels("anpr", "ok").inc()
            return resp.json()
        log.info("anpr_eval_miss", status=resp.status_code)
    except httpx.HTTPError as exc:
        log.warning("anpr_eval_unreachable", url=url, error=str(exc))
    raise HTTPException(status_code=503, detail={"error": "anpr_eval_unavailable"})
```

### 2b. `GET /api/traffic/metrics` → expose congestion F1
Append to [`gateway/routers/traffic.py`](gateway/routers/traffic.py) (proxy `ai/congestion` metrics or read the artifact JSON the model writes):

```python
@router.get("/metrics")
async def metrics(state: GatewayState = Depends(get_state)) -> dict:
    """Congestion-onset F1 for the dashboard realism probe
    (web/src/data/live.ts:congestionMetrics)."""
    url = state.cfg.congestion_ai_url.rstrip("/") + "/metrics"  # add congestion_ai_url to config
    try:
        resp = await state.http.get(url, timeout=10.0)
        if resp.status_code == 200:
            return resp.json()  # {"f1": ..., "target_f1": 0.85}
    except httpx.HTTPError as exc:
        log.warning("congestion_metrics_unreachable", url=url, error=str(exc))
    raise HTTPException(status_code=503, detail={"error": "congestion_metrics_unavailable"})
```

> The dashboard already handles 404/503 by degrading to `null`, so these routes are purely additive — no frontend change required.

---

## §3 (P0) — Make real OCR the production default  ·  ~4 h

**Problem:** `ingest/anpr` `DRY_RUN=True` default ([`config.py:55`](ingest/anpr/src/anpr_ingest/config.py#L55)) → real inference skipped.

**Fix A — env:** set `DRY_RUN=false` in `.env.aws.example` and the prod compose/helm values.

**Fix B — startup guard** (prevents silent synthetic OCR in prod). Add to `ingest/anpr` startup (mirrors `gateway/auth.py:validate_auth_config`):

```python
def validate_anpr_config(cfg) -> None:
    env = os.environ.get("APP_ENV", "development").strip().lower()
    prod_like = env not in {"development", "dev", "local", "test"}
    if prod_like and cfg.dry_run:
        raise RuntimeError(
            f"DRY_RUN must be 'false' in the '{env}' environment: refusing to "
            "start the ANPR ingest with synthetic OCR. Set DRY_RUN=false."
        )
```

Call it in the ingest entrypoint before the capture loop.

---

## §4 (P0) — Give the Driver PWA an authenticated identity  ·  ~12–16 h

**Problem:** PWA sends no `Authorization` header; prod gateway enforces auth → 401.

**Steps:**
1. **Mint a DRIVER token at pairing.** Backend already has `POST /api/auth/dev-token` (`auth.py:82`) accepting `{role, device_id}`. For production, add a pairing-bound issue path (device code → short-lived device-scoped JWT). Reuse the HS256 signer in `gateway/auth.py`.
2. **Store & attach.** In `mobile-pwa/src/lib/device.ts` persist the token; in `mobile-pwa/src/lib/api.ts` attach it:

```ts
import { getToken } from "./device";

async function http<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getToken();
  const res = await fetch(path, {
    headers: {
      "content-type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init?.headers || {}),
    },
    ...init,
  });
  // …unchanged…
}
```

3. **Add a DRIVER-scoped RBAC policy** in `gateway/auth.py` `_POLICY` for the PWA endpoints, ideally constraining `{device_id}` in the path to the token's `device_id` claim:

```python
"/api/trucks":   {"DRIVER", "JNPA_TRAFFIC", "DTCCC_ADMIN", "TERMINAL_OPS"},
"/api/vahan":    {"DRIVER", "JNPA_TRAFFIC", "DTCCC_ADMIN", "TERMINAL_OPS", "CUSTOMS"},
"/api/alerts":   {"DRIVER", "JNPA_TRAFFIC", "DTCCC_ADMIN", "TERMINAL_OPS"},
# enforce device-scope in handler: principal.device_id == path device_id
```

4. **WS auth:** pass the token on the `/api/ws` connect (`workers/realtime.worker.ts` `connect` message) and validate it server-side.

---

## §5 (P0) — Evidence images to object storage  ·  ~6–8 h

**Problem:** `anpr_reads.image_url` is a base64 data-URL in DRY_RUN.

**Fix:** in `ingest/anpr/emit.py` (non-DRY_RUN path) and/or `ai/anpr`, persist the plate crop to MinIO/S3 (the `evidence` bucket already used by `ai/anomaly`, `anomaly/storage.py:put_evidence`) and write the **resolved URL** into `anpr_reads.image_url`. Ensure `reports.py` police `evidence_url` resolves to that store.

---

## §6 (P1) — Real-feed cutover plan  ·  ~ external, multi-day

Keep simulators behind the `jnpa.services(name, kind='sim'|'live')` registry. Register real telematics / RFID / RTSP-ANPR as `kind='live'`; the existing fallback orchestrator demotes to `sim` only on outage. No frontend change. Track per-integration in a separate epic.

---

## §7 (P2) — Cleanup  ·  ~4 h
- Wire a dashboard ANPR-reads view to `/api/anpr/read/{camera_id}` **or** mark `/infer,/read,/cameras` internal/retire (GAP §7).
- Consolidate camera health (`/api/kpi/cameras` vs `/api/anpr/cameras`).
- Add `ABANDONED`, `ROUTE_DEVIATION` to dashboard mock kinds (`web/src/data/mock.ts`).
- Remove unused PWA `fastag()` client method or wire it into Profile.
- Confirm `/api/parking/summary` returns `total_capacity/total_available/facilities` keys the PWA expects.

---

## Effort summary

| # | Item | Priority | Est. hours |
|---|---|---|---|
| 1 | Vahan field fix | P1 | 1 |
| 2 | Missing proxy routes | P1 | 3 |
| 3 | Real-OCR default + guard | P0 | 4 |
| 4 | PWA auth + DRIVER RBAC | P0 | 12–16 |
| 5 | Evidence object storage | P0 | 6–8 |
| 6 | Real-feed cutover (code scaffolding) | P1 | 8–12 |
| 7 | Cleanup | P2 | 4 |
| | **Total (excl. 3rd-party integration work)** | | **~38–52 h** |
