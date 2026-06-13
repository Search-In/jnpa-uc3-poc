# JNPA UC-III — What-If Scenarios + Reactive Workflow (Sub-Criterion 5)

Three named bid scenarios, each triggerable from the dashboard's What-If
Console, visible end-to-end as an automated downstream chain, reversible via
"Reset to baseline", and logged as a `Scenario` row (with a per-step audit) so it
can be replayed.

## Scenarios

| Name   | Trigger params                       | Chain |
| ------ | ------------------------------------ | ----- |
| `tfc1` | `{gate_id, duration_minutes}`        | close gate → inject `AT_GATE_QUEUE` build-up → forecaster predicts spillover (P≥0.7) → auto-re-route inbound trucks to best alt gate → TAS slots `RESCHEDULED` |
| `tfc2` | `{camera_id}`                        | inject wrong-way track → anomaly `WRONG_WAY` alert → e-Challan stub (plate via Vahan fallback chain) → stamp `echallan_id`/`pdf_url` on alert → evidence MP4 in drawer |
| `tfc3` | `{dpd_release_spike}`                | publish `cargo.dpd_release` (cross-twin) → `uc2_bridge` → instantiate corridor trucks → forecaster build-up on segments 8-14 (≥5 ≥P0.6) → reissue gate-slot windows + PWA push → labelled cross-twin arrow |

Each module exposes `async def run(params) -> ScenarioHandle` and
`async def reset(handle) -> None`, registered in `scenarios/__init__.py`
(`REGISTRY`) and mirrored as `jnpa.scenarios` entry-points in `pyproject.toml`.

## Runner service (port 8400)

```
POST /scenarios/{name}/run     -> {handle_id, ...}
POST /scenarios/{name}/reset   -> {ok}            # body {handle_id?} optional
GET  /scenarios/{handle_id}/timeline -> event-by-event log
GET  /scenarios                -> registered scenarios + running handles
```

Every step is persisted to `jnpa.scenario_steps`, mirrored into
`jnpa.scenarios.params.steps[]` (audit, with each step's trigger source), and
pushed to `/api/ws` as `type=scenario_step` (the gateway fans it to dashboard
sockets). Every scenario emits an OpenTelemetry trace to Jaeger
(`jnpa_shared.tracing`) spanning ingest → AI → alert → action.

## Reactive-workflow guarantees

- **Idempotent** steps; each records its `trigger`.
- **Reset to baseline** restores: gate state (`closed_at = NULL`), injected
  trucks removed (`DELETE /devices/tagged/{tag}`), synthetic alerts `ack`-ed,
  synthetic `traffic_snapshots` deleted, and Redis caches re-warmed by forcing a
  fresh forecaster poll.

## Forecast assertions (best-effort + nudge)

The trained forecaster won't always cross P-thresholds from a short injected
build-up, so scenarios *nudge* the relevant corridor segments (write high-jam
`traffic_snapshots`), poll `/predict`, and record the assertion as `met` /
`degraded` in the timeline without hard-failing the run — so the demo stays
green while the assertion is still visible.

## Verify

```bash
curl -s -XPOST http://localhost:8400/scenarios/tfc1/run \
  -d '{"gate_id":"G-NSICT","duration_minutes":120}' -H 'content-type: application/json'
# -> {"handle_id":"tfc1-...", ...}
curl -s http://localhost:8400/scenarios/<handle_id>/timeline | python3 -m json.tool
# then open http://localhost:3000/whatif and watch the timeline; Jaeger: http://localhost:16686
```
