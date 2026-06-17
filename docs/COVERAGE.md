# JNPA Use Case III — Coverage Matrix

> Tender **GEM/2026/B/7297343** · Appendix C §2.3 (Corrigendum 3, pp. 42–44) · Bid §8.5 · D.2 PoC = 10 marks.
>
> This is the Phase-0 audit matrix, kept current as remediation lands. It doubles as evidence
> for **D.2 sub-criterion 1** (solution approach + assumptions). Each row scores honestly against
> the repository as it stands. Legend: **Y** = present & demonstrable · **Partial** = real but
> incomplete · **N** = not yet built.

## How to read the "Demonstrate" column

Every Y/Partial row names a concrete way to see it live — an endpoint, a dashboard screen, or a
`scripts/poc-selftest` line. Run `python -m scripts.poc_selftest` for a one-shot pass/fail per row.

---

## A. Appendix C — Use Case III Requirements (8)

| # | Requirement | Where (App C / Bid) | Present | File(s) | Demonstrate |
|---|---|---|---|---|---|
| C1 | Mobile app + Digital Twin: routing guidance, **parking availability in geo-fenced port**, congestion heatmap of approach routes | App C §2.3 #1 | **Y** | `mobile-pwa/`, `web/src/screens/LiveOperations.tsx`, `web/src/components/map/*`, `parking/` | Dashboard heatmap + Parking board; mobile reroute screen |
| C2 | PDP **face-recognition** + **Vahan & Sarathi** integration | App C §2.3 #2 | **Y** | `identity/`, `ingest/vahan_sim/`, `gateway/routers/vahan.py`, `gateway/routers/identity.py` | `POST /api/identity/verify` (synthetic faces), Vahan chain |
| C3 | **Empty-container** supply–demand optimiser + probable allocation across fleet owners / shipping line / CFS / ECD; tanker / break-bulk / cement-bowser variants | App C §2.3 #3 | **Y** | `empty-container/`, `gateway/routers/empty_container.py` | `GET /api/empty/allocations`; Empty-Container board; drives TRT-empty KPI |
| C4 | **Alerts & notifications for Customs** (compliance / flags) | App C §2.3 #4 | **Y** | `gate-data/`, `gateway/routers/gate_data.py`, `gateway/routers/alerts.py` | `CUSTOMS_FLAG` alerts on dashboard feed; Auto-LEO panel |
| C5 | **e-seal, Form 13, weighbridge, ICEGATE** capture → **Auto-LEO** + container/vehicle ID match | App C §2.3 #5 | **Y** | `gate-data/`, `gateway/routers/gate_data.py` | `POST /api/gate-data/leo`; Auto-LEO panel shows reconciled record |
| C6 | **Carbon-emissions** calc (fleet API + CPP / parking dwell, AoI rollup) | App C §2.3 #6 | **Y** | `carbon/`, `gateway/routers/carbon.py` | `GET /api/carbon/rollup`; Carbon tile on dashboard |
| C7 | **AI video-analytics**: detection, classification, OCR, trajectory tracking, density heatmaps, ETA | App C §2.3 #7 | **Y** | `ai/anpr/`, `ai/congestion/`, `ai/anomaly/` | `/eval`, `/metrics` on each AI service |
| C8 | **Geofencing + no-parking-zone violation** + automated alerts & notifications | App C §2.3 #8 | **Y** | `ai/anomaly/rules/parking.py`, `web/src/screens/GeofencingManager.tsx` | Illegal-parking escalation; zone editor |

## B. Appendix C — KPIs (acceptance criteria)

| KPI | Present | File(s) | Demonstrate |
|---|---|---|---|
| Gate Queue Wait Time (min/hr) | **Y** | `shared/jnpa_shared/kpi.py`, `web/src/kpi/` | KPI strip card; `{value,target,deltaPct,trend}` |
| Avg Gate Transaction Time (min/hr) | **Y** | `shared/jnpa_shared/kpi.py`, `web/src/kpi/` | KPI strip card |
| TRT for empty containers from ECD (min/hr) | **Y** | `empty-container/`, `shared/jnpa_shared/kpi.py` | KPI strip card; fed by allocation timings |
| Turn Around Time inside port (min/hr) | **Y** | `shared/jnpa_shared/kpi.py`, `web/src/kpi/` | KPI strip card |
| Queue length / avg dwell / gate throughput (bid §8.5.4) | **Y** | `shared/jnpa_shared/kpi.py`, `gateway/routers/kpi.py` | KPI strip + gate panels |

Every KPI returns `{ value, target, deltaPct, trend[] }` against a configurable baseline
(% improvement vs. current baseline ops). Pure functions, unit-tested — see
[KPI_DEFINITIONS.md](KPI_DEFINITIONS.md).

## C. D.2 PoC marking scheme (2 marks each)

| # | Criterion | Present | Demonstrate |
|---|---|---|---|
| D1 | Solution approach / methodology + **assumptions listed in-app** | **Y** | [ASSUMPTIONS.md](ASSUMPTIONS.md) + in-app "Assumptions & Methodology" panel |
| D2 | Usage of **AI/ML tools** | **Y** | 4 model services; `/eval` + `metrics.json` thresholds |
| D3 | API / data-integration + **fallback on data unavailability** | **Y** | 3 fallback chains; `/api/kpi/sources` Health Cards; `/api/debug/decisions` |
| D4 | **Dashboard view & KPI monitoring** | **Y** | DTCCC dashboard; KPI strip with targets/deltas |
| D5 | **What-if scenarios** + interdependency + **automated reactive workflow** | **Y** | TFC-1/2/3; TFC-3 = cross-twin UC2→UC3 |

## D. Bid §8.5 commitments

| Clause | Present | Notes |
|---|---|---|
| §8.5.1 data sources (ANPR/OCR ≥95%; Vahan/Sarathi/FastTag sim; RFID; Trucking 20k→30k) | **Y / see note** | All sources wired. OCR ≥95% requires model weights loaded; runs in deterministic fallback on a CPU-only PoC host (state surfaced honestly in `/eval`). |
| §8.5.2 models (CNN+CRNN; GNN+LSTM **F1≥0.85**; ByteTrack+rule+AE; ETA) | **Y / see note** | All three model architectures present. Congestion F1 = **0.8411** (`ai/congestion/artifacts/metrics.json`) — marginally **under** the 0.85 target; flagged as a WARN by `scripts/poc-selftest` (B.1), closeable by a retrain/retune, not an architecture gap. |
| §8.5.3 fallbacks (camera live→cached→synthetic; Vahan→cached→PROVISIONAL 24h; Trucking GPS→ULIP→web check-in) | **Y** | `gateway/fallback.py`; all three chains. |
| §8.5.4 dashboard (40-km corridor heatmap; geofence escalation; **police reports**) | **Y** | `web/src/screens/PoliceReports.tsx`; PDF export. |
| §8.5.5 scenarios (TFC-1/2/3 incl. cross-twin) | **Y** | `scenarios/tfc1.py`, `tfc2.py`, `tfc3.py`, `uc2_bridge.py`. |

---

## Architecture parity vs. Use Case 1 standard

| UC1 standard | This repo | Status |
|---|---|---|
| ArcGIS Maps SDK 5.x (`<arcgis-map>`) + Calcite dark shell | `web/` migrated to `@arcgis/core` + `@arcgis/map-components` + Calcite | **Y** (post-migration) |
| Single typed data adapter, `mock\|live` switch | `web/src/data/` `MockAdapter`+`LiveAdapter`, `VITE_DATA_MODE` | **Y** |
| AI behind gateway; UI never calls models directly | Gateway mediates every upstream | **Y** |
| Tested KPI engine `{value,target,deltaPct,trend}` | `shared/jnpa_shared/kpi.py` + `web/src/kpi/` + tests | **Y** |
| Documented fallback chains + on-screen Health Cards | `/api/kpi/sources`; SystemHealth screen | **Y** |
| `.env.example` committed; secrets only in `.env` | `.env.local.example`, per-app `.env.example` | **Y** |
| No colour literals outside tokens | Calcite tokens + `tokens.ts` | **Y** |

## Cross-cutting quality

| Item | Status |
|---|---|
| TS strict, ESLint + Prettier | **Y** |
| Vitest (KPI fns + adapter contract) | **Y** |
| pytest (model metric thresholds) | **Y** |
| Graceful empty/error states (Calcite notices) | **Y** |
| Responsive to tablet | **Y** |
| Keyboard focus + reduced-motion | **Y** |
| Multilingual EN / HI / MR scaffolding (Corr. 3 App A6) | **Y** |
| `scripts/poc-selftest` per-requirement pass/fail | **Y** |
</content>
</invoke>
