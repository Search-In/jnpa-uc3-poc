# Phase 2 — Customs & Gate Systems (Compliance Report)

**Track:** e-Seal · Form-13 · Weighbridge · ICEGATE · Auto-LEO / Customs alerts
**Date:** 2026-07-07 · **Result:** Backend + persistence + audit **live-validated**; Web UI **code-complete (tsc-clean), pending image build**.

Built on the Phase-1 RDS audit framework — the audit layer itself was **not modified**.

## What was implemented

| Layer | Deliverable |
|---|---|
| **DB schema** | `jnpa.gate_captures`, `jnpa.leo_reconciliation` (migration `0004_gate_customs.sql` + `init.sql`; idempotent, indexed by container/plate/type/timestamp) |
| **Persistence** | `gate-data/persistence.py` — capture upsert, reconciliation, durable customs-alert→`jnpa.alerts` (+`digital_twin_events` mirror), api-audit writer, RDS read paths |
| **Provider adapter** | `gate-data/providers.py` — per-source **SIM \| LIVE** (env `GATE_<SRC>_MODE`/`_URL`); LIVE fetches over HTTP and logs every request/response to `jnpa.api_audit_log`; degrades to SIM if unconfigured |
| **Service wiring** | `gate-data/app.py` — boot-time persistence of the full capture inventory + reconciliations + customs alerts (background, idempotent); `POST /leo` persists; new `/captures`, `/reconciliations`, `/customs/history`, `/providers` endpoints |
| **Gateway** | `gateway/routers/gate_data.py` — proxies the four RDS-backed read endpoints |
| **Web UI** | `web/src/screens/GateCustoms.tsx` — Captures (e-Seal/Form-13/Weighbridge/ICEGATE tabs) · Auto-LEO (ready/blocked) · Customs Flags feed; per-source SIM/LIVE badges. Routed at `/gate-customs`, nav item + RBAC (`CONTROL_ROOM`+`CUSTOMS`) + en/hi/mr labels |

## Six-dimension status (was → now)

| Module | FE UI | BE API | DB persist | Real vs Sim | Audit trail | Prod ready |
|---|---|---|---|---|---|---|
| e-Seal | ❌→✅* | 🟡→✅ | ❌→✅ `gate_captures` | ❌→🟡 SIM, LIVE-pluggable | ❌→✅ | ❌→🟡 |
| Form-13 | ❌→✅* | 🟡→✅ | ❌→✅ `gate_captures` | ❌→🟡 | ❌→✅ | ❌→🟡 |
| ICEGATE | ❌→✅* | 🟡→✅ | ❌→✅ `gate_captures` | ❌→🟡 | ❌→🟡 (EDI later) |
| Weighbridge | ❌→✅* | 🟡→✅ | ❌→✅ `gate_captures` | ❌→🟡 | ❌→✅ | ❌→🟡 |
| Customs Alert | ✅ | ✅ | ❌→✅ `alerts`+`digital_twin_events` | ❌→🟡 | ❌→✅ | ❌→🟡 |
| Auto-LEO | 🟡→✅* | ✅ | ❌→✅ `leo_reconciliation` | 🟡→✅ | ❌→✅ |

*Web screen is code-complete + type-checked; deploys on the next `docker compose build web`.

## Live validation (running stack)

| Check | Evidence |
|---|---|
| Every capture in RDS | `gate_captures = 808` (ESEAL/FORM13/ICEGATE/WEIGHBRIDGE = 202 each) |
| Idempotent re-seed | captures stay **808** across repeated gate-data boots (upsert on container+type+captured_at); migration 0004 re-runs clean |
| Reconciliations persisted | `leo_reconciliation` rows written (append-per-run audit history) |
| Customs alerts durable | `alerts(kind=CUSTOMS_FLAG) = 38` — ESEAL_TAMPER:16, WEIGHT_MISMATCH:14, LEO_MISSING:8 → **now visible to `/api/reports/police`** |
| Customs alerts on event timeline | `digital_twin_events(CUSTOMS_ALERT) = 41` (mirrored) |
| API audit | `api_audit_log` grows on every gateway→gate-data call; LIVE deepest-hop wired to same table |
| Gateway read APIs | `/api/gate-data/{providers,captures,reconciliations,customs/history}` all return RDS rows (200) |
| Survives restart | data intact after gate-data restarts **and** two Postgres crash-recoveries |
| Web typecheck | `tsc -b --noEmit` exit 0 |

## Provider (SIM → LIVE) cutover — no redesign needed

Each source goes live independently by setting two env vars, e.g.:
```
GATE_ICEGATE_MODE=live   GATE_ICEGATE_URL=https://icegate.gov.in/edi-endpoint
```
The service then fetches that source over HTTP, logs the full request/response to
`jnpa.api_audit_log`, and tags captures `source_mode='live'`. Unset/failed LIVE
sources fall back to SIM automatically. Current state: all four = SIM (no real
endpoints provisioned). ICEGATE will additionally need EDI message/cert handling.

## ⚠️ Environment note (not a code defect)

The local Docker VM is **3.8 GiB total for ~28 containers**. Postgres/TimescaleDB
was **OOM-killed twice** under the concurrent insert load (the 808-row boot burst
+ continuous ANPR ingest). Data recovered intact each time (durability held).
`jnpa-anpr-ingest` is currently **stopped** to keep Postgres stable for validation.

**Remediation:** raise Docker Desktop memory to ≥ 8 GiB, then
`docker start jnpa-anpr-ingest`. For production/RDS this is a non-issue (managed
instance sizing). Recommend a small Postgres `shared_buffers`/`work_mem` cap for
local dev.

## To deploy the Web UI
```bash
docker compose build web && docker compose up -d web    # picks up GateCustoms screen
# then open http://localhost:3000/gate-customs
```

## Remaining gaps (this track)
- Real vendor endpoints for e-Seal / Form-13 / Weighbridge and ICEGATE **EDI** (adapter + persistence + audit are ready; only endpoints/credentials + ICEGATE EDI codec remain).
- Web image rebuild to surface the screen (backend already serving).
- Mobile PWA customs-alert view (Phase 2 §8 — separate track).
