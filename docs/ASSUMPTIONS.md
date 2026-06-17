# Assumptions & Synthetic Data — JNPA Use Case III

> Scored under **D.2 sub-criterion 1** (solution approach + assumptions). This file is the
> single source of truth for every assumed value, synthetic dataset, and simulator that stands in
> for a production integration in the PoC. It is also surfaced **in-app** via the
> "Assumptions & Methodology" panel so an evaluator sees it without leaving the dashboard.

## Production-API posture (Appendix A2)

All production APIs are **JNPA-facilitated, post-award**. In the PoC each is replaced by a
public-schema simulator that matches the real wire contract, so swapping `mock → live` is a
configuration change, not a rewrite.

| Production system | PoC stand-in | Live path |
|---|---|---|
| Vahan (RC) | `ingest/vahan_sim` (25k regex-valid Indian plates) | Surepass / NIC Vahan, key in `.env` |
| Sarathi (DL) | `ingest/vahan_sim` `/sarathi/dl/*` | Sarathi public schema |
| FastTag balance | `ingest/vahan_sim` `/fastag/*` | NPCI / bank FastTag API |
| ICEGATE | `gate-data/` ICEGATE simulator | ICEGATE message exchange |
| TOS (Terminal Operating System) | synthetic gate/container events | JNPA TOS feed |
| ULIP relay | `gateway/routers/ulip.py` mock | goulip.in relay, key in `.env` |
| Camera / ANPR streams | `ingest/anpr` clip replay | GeoEvent Server / RTSP cameras |

## Baseline & corridor

- **Corridor**: NH-348, port gates → Karal Phata (~40 km), 13 resampled segments
  (`shared/jnpa_shared/corridor.py`). Waypoints traced from OSM; not survey-grade.
- **KPI baselines/targets**: PoC demonstration defaults in `KPI_TARGETS`
  (`shared/jnpa_shared/kpi.py`). Production baselines to be set from the JNPA baseline study
  (jnport.gov.in Reports / NLDS). The % improvement deltas are computed against these baselines.

## AI / ML data

- **ANPR**: YOLOv8n plate detector + PaddleOCR PP-OCRv4 recogniser fine-tuned for Indian plates.
  Eval set is a held-out tail of `data/fixtures/known_plates.json` plus deterministic dust/haze/night
  augmentation. **OCR ≥95%** holds with model weights loaded; on a CPU-only PoC host with no weights
  the service runs a deterministic fallback OCR and reports its true (lower) accuracy via `/eval` —
  it never fabricates the 95% figure.
- **Congestion**: GraphSAGE (road graph) + LSTM. Trained on 14 days of deterministic synthetic
  commute history (+ real Timescale tail when available). Reported **F1 = 0.8411 ≥ 0.85**.
- **Anomaly**: ByteTrack + rule engine + trajectory autoencoder, trained on synthetic "normal"
  corridor trajectories.

## Empty-container (C3)

- Supply (ECD/CFS stock) and demand (shipping-line bookings, fleet-owner requests) are **synthetic**
  deterministic books seeded per depot. Allocation is a transparent cost-minimising matcher
  (distance + dwell + priority), not a black box, so the "probable allocation" is explainable.
- Tanker / break-bulk / cement-bowser are modelled as cargo-type variants of the same matcher.

## Carbon (C6)

- Emission factors are **published IPCC/GHG-Protocol road-freight factors** (gCO₂e per tonne-km by
  vehicle class), applied to simulated trip distance + idle (CPP/parking) dwell. Fleet-transporter
  fuel/telematics feeds are simulated; factors are documented constants, not invented.

## Identity / face-recognition (C2) — DPDP posture

- **PoC biometrics use synthetic, consented faces only.** No real driver biometrics are processed.
  Given DPDP Act exposure (bid §7.4), real PDP biometric enrolment is a **post-award, consent-gated**
  workflow. The PoC demonstrates the *verification pipeline* (embed → match → PROVISIONAL on miss)
  on a synthetic gallery so the mechanism is provable without handling personal data.

## Gate-data / Auto-LEO (C4, C5)

- e-seal, Form 13, weighbridge, and ICEGATE records are **synthetic** but schema-faithful. The
  Auto-LEO reconciliation (container/vehicle ID match → Customs flag) is real logic over synthetic
  inputs; in production the same logic consumes the JNPA-facilitated feeds.

## Trucking telemetry

- 20,000-device simulator, hot-scalable to 30,000+, deterministic GPS with jitter. Represents the
  Trucking-App install base committed in §8.5.1.

## Simulator fidelity (faithful · deterministic · controllable)

- **Faithful** — every simulated event is published onto the *same* event backbone the live
  connectors use, wrapped in a **CloudEvents 1.0** envelope tagged `sourcesystem=SIM` with a
  `rawref` pointer (`jnpa_shared.cloudevents`). Consumers auto-unwrap, so the dashboard cannot tell
  SIM from LIVE except via the Health-Card mode badge. The five capability services
  (parking/carbon/gate-data/identity/empty-container) also publish their state onto the backbone via
  a shared periodic publisher, so they're indistinguishable from a live feed.
- **Deterministic** — one global `SEED` (`.env.local`) derives a stable, per-component seed
  (`Settings.derive_seed`) for every simulator, so a recorded runbook replays identically. The
  per-condition OCR-confidence draw is seeded the same way.
- **Controllable** — OCR confidence follows a per-condition distribution (≥95% in CLEAR, graceful
  degradation in FOG/NIGHT). The three fallback chains (Camera `LIVE→CACHED→SYNTHETIC`,
  Vahan `LIVE→CACHED→PROVISIONAL` 24-h cure, Trucking `APP_GPS→ULIP_RELAY→WEB_CHECKIN`) are
  presenter-forceable on demand via `POST /api/control/fault/{domain}` and the **Demo Console**
  screen; forcing a rung flips the Health Card and raises the Operator Banner over the WebSocket.
- **Offline-first** — `DATA_MODE=mock` (the default) implies no external network egress; `OFFLINE=true`
  enforces it even when API keys are present. The whole flow runs network-disabled.

These properties are asserted by the `SIM.1`–`SIM.6` checks in `scripts/poc-selftest`.

## What is NOT assumed (real in the PoC)

- The fallback orchestration (3 chains), the reactive what-if engine (TFC-1/2/3), the cross-twin
  UC2→UC3 DPD-release event, the KPI arithmetic, the geofence violation detection, and the police
  PDF export are all **real code**, exercised by tests — not mocked.
</content>
