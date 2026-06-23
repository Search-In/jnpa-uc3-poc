# PWA_DASHBOARD_ALIGNMENT_REPORT.md

**Project:** JNPA UC-3 Port Traffic Digital Twin
**Question:** Are the Driver PWA and the Main Dashboard aligned on workflow, status values, and enums?
**Date:** 2026-06-23

> **Architectural reminder:** the two apps are *not* mirror images. The Dashboard is a control-room console (write/command surface); the Driver PWA is a per-device advisory client (read + acknowledge). They intersect on the **truck/reroute/alert/vehicle** domain. Alignment is judged on the *shared vocabulary*, not on feature parity.

---

## 1. Shared business workflow

| Workflow | Dashboard role | PWA role | Aligned? |
|---|---|---|---|
| Re-route advisory | Operator issues via `POST /api/trucks/{id}/route` (DriverAdvisory screen) | Driver receives via WS `reroute` + fallback `GET …/route/latest`, then `POST …/route/ack` | ✅ Same advisory object, same channel |
| Alerts | Operator views/triages all alerts; evidence dialog | Driver sees own alerts in Inbox; `POST …/ack` | ✅ Same `Alert` shape |
| Vehicle identity | Operator sees plate→RC in Police Reports | Driver sees own RC in Profile | ⚠️ Same endpoint, **field-name mismatch** (see §7) |
| Truck position | Operator map (all trucks, WS `truck_position`) | Driver single-device trip view | ✅ Same `TruckDevice` record |
| TAS slot | Not surfaced on dashboard | Driver sees next slot window | ⚠️ PWA-only; no shared surface |
| Parking | Operator availability map | Driver parking summary | ✅ Same domain, different granularity |

---

## 2. Status / decision-path enums

The system does **not** use a `pending → processed → verified` lifecycle. It uses **fallback-rung** vocabularies. Both apps and the backend agree exactly:

| Enum | Backend source | Dashboard | PWA | Aligned? |
|---|---|---|---|---|
| ANPR path | `LIVE / CACHED / SYNTHETIC` (`fallback.py:51`) | ✅ (`types.ts`, SystemHealth) | n/a (PWA has no ANPR) | ✅ |
| Vahan path | `LIVE_PRIMARY / LIVE_FALLBACK / CACHED / PROVISIONAL` (`fallback.py:57`) | ✅ (fault rungs `mock.ts:593`) | ✅ (`types.ts:88`) | ✅ |
| Truck path | `PRIMARY / SECONDARY / TERTIARY` (`fallback.py:64`) | ✅ | ✅ (`types.ts:13`) | ✅ |
| Source health | `LIVE / DEGRADED / DOWN` (`fallback.py:130`) | ✅ | n/a | ✅ |
| Identity | `VERIFIED / PROVISIONAL / REJECTED` | ✅ | n/a | ✅ |

---

## 3. Alert severity values

| Severity literal | Backend (`alerts`) | Dashboard `types.ts:5` | PWA `types.ts` | Aligned? |
|---|---|---|---|---|
| `info` | ✅ | ✅ | ✅ | ✅ |
| `warning` | ✅ | ✅ | ✅ | ✅ |
| `critical` | ✅ | ✅ | ✅ | ✅ |
| `REPORT_TO_POLICE` | ✅ | ✅ | ✅ (drives "challan" categorisation) | ✅ |

**Aligned.** PWA additionally derives a UI category `reroute | alert | challan` from `kind`/`severity` (`RealtimeContext.tsx:64`) — a presentation concern, not a contract divergence.

---

## 4. Alert `kind` values

Backend emits: `CUSTOMS_FLAG, PROVISIONAL_VEHICLE, WRONG_WAY, ILLEGAL_PARKING, ABANDONED, ROUTE_DEVIATION, ELEVATED_SCRUTINY`.
Dashboard `mock.ts` enumerates a subset (`WRONG_WAY, ILLEGAL_PARKING, PROVISIONAL_VEHICLE, ELEVATED_SCRUTINY, CUSTOMS_FLAG`); PWA treats `kind` as an open string and only special-cases `*CHALLAN*`.

⚠️ **Minor:** Dashboard mock omits `ABANDONED` and `ROUTE_DEVIATION`. Both apps tolerate unknown kinds (open string), so no runtime break — but the dashboard's mock fixtures under-represent the backend's kind set. **P2.**

---

## 5. Truck states

Backend / simulator: `EN_ROUTE_TO_PORT, AT_GATE_QUEUE, INSIDE_PORT, GATE_OUT, EN_ROUTE_TO_ECD` (+ `EN_ROUTE_HOME, IDLE` in the fleet sim).

| State | Dashboard `mock.ts:175` | PWA | Aligned? |
|---|---|---|---|
| `EN_ROUTE_TO_PORT` | ✅ | reads `record.state` (open string) | ✅ |
| `AT_GATE_QUEUE` | ✅ (DriverAdvisory filter) | open | ✅ |
| `INSIDE_PORT` | ✅ | open | ✅ |
| `GATE_OUT` | ✅ | open | ✅ |
| `EN_ROUTE_TO_ECD` | ✅ | open | ✅ |

✅ Dashboard has the full enum; PWA consumes it as an opaque label (no hardcoded list to drift). Aligned.

---

## 6. Route states & congestion states

- **Route ACK state:** PWA sends `"ACK" | "DECLINE"` (`api.ts:50`); backend `ack_reroute` accepts the same. ✅
- **TAS slot status:** PWA reads `"BOOKED" | "RESCHEDULED" | "CANCELLED"` (`types.ts:61`); backend `tas_mock` produces the same. Dashboard does not surface TAS. ✅ (PWA-only)
- **Congestion:** expressed as continuous `jam_factor` (0–10) + per-segment `P(congested)` float, not a discrete enum. Both apps treat it numerically. ✅ No enum to mismatch.

---

## 7. Vehicle states — the one real mismatch

`GET /api/vahan/rc/{plate}` → `VahanRecord`. The Dashboard renders the masked/canonical fields it actually queries (Police Reports uses `rc.vehicle_class`, owner) and stays aligned. The **PWA Profile reads legacy field names that the backend does not emit**:

| PWA reads | Backend emits | Outcome |
|---|---|---|
| `maker`, `model` | — (absent) | empty |
| `insurance_upto` | `insurance_valid_to` | empty |
| `fitness_upto` | `fitness_valid_to` | empty |

→ **PWA and Dashboard are NOT aligned on the Vahan consumer contract.** Dashboard = aligned; PWA = 3 stale field names. Fix: [FIX_PLAN.md](FIX_PLAN.md) §1.

---

## Alignment verdict

| Dimension | Verdict |
|---|---|
| Business workflow | ✅ Aligned |
| Status / decision-path enums | ✅ Aligned |
| Alert severity | ✅ Aligned |
| Alert kind | ⚠️ Dashboard mock subset (P2) |
| Truck states | ✅ Aligned |
| Route states | ✅ Aligned |
| Congestion states | ✅ Aligned (numeric) |
| Vehicle (Vahan) fields | ❌ PWA uses stale names (P1) |

**Overall: substantially aligned.** One genuine mismatch (Vahan field names in the PWA) and one cosmetic one (dashboard mock kind subset). No enum-value divergence anywhere in the shared status vocabulary.
