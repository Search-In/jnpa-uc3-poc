# gate-data — gate capture + Auto-LEO reconciliation

A FastAPI service (port **8350**) implementing Appendix C requirements **#4**
and **#5** for the JNPA UC-III PoC: capture of **e-seal**, **Form 13**,
**weighbridge** and **ICEGATE** data per export container/vehicle pair, the
**container/vehicle identity match**, and the **Customs alerts & flags** that
gate an automated **Let Export Order (LEO)**.

Like [`ingest/vahan_sim`](../ingest/vahan_sim/), the dataset is deterministic
and schema-faithful, so the rest of the system is API-correct before the real
ICEGATE / e-seal integrations are provisioned.

## Endpoints

| Method | Path                          | Returns        |
| ------ | ----------------------------- | -------------- |
| GET    | `/healthz`                    | liveness + container count |
| GET    | `/metrics`                    | Prometheus exposition |
| GET    | `/records/{container_no}`     | the four raw captured source records |
| POST   | `/leo`                        | body `{container_no}` -> `AutoLeoResult` |
| GET    | `/leo/queue`                  | reconcile every container (Auto-LEO panel feed) |
| GET    | `/customs/flags`              | all current Customs flags (Customs feed) |

## Captured records

For each container/vehicle pair, `seed.py` deterministically generates four
source records the Auto-LEO process reconciles:

| Record       | Key fields |
| ------------ | ---------- |
| e-seal       | `eseal_id`, `container_no`, `status`, `tamper_flag` |
| Form 13      | `form13_no`, `container_no`, `shipping_bill_no`, `cargo_desc`, `gross_wt_kg` |
| weighbridge  | `vehicle_plate`, `container_no`, `measured_wt_kg`, `axle_count` |
| ICEGATE      | `shipping_bill_no`, `leo_status`, `igm_no`, `assessment` |

Container numbers follow the **ISO 6346**-ish format (3 line letters + `U` + 7
digits, e.g. `MSCU1234567`); vehicle plates reuse the canonical Indian-plate
format so the gate data joins cleanly against the Vahan dataset. Everything is
derived from a fixed `SEED` anchored to a fixed `REFERENCE_DATE`, so results are
identical across runs and hosts.

## Auto-LEO reconciliation

`leo.reconcile(container_no)` is a **pure function** that joins the four records
by `container_no` / vehicle plate and runs the LEO checks. A container is
`leo_ready` only when every check passes; each failure raises a Customs flag:

| Check                                  | Customs flag       |
| -------------------------------------- | ------------------ |
| container/vehicle identity records join | `ID_MISMATCH`      |
| e-seal present and not tampered         | `ESEAL_TAMPER`     |
| weighbridge weight within tolerance (2%)| `WEIGHT_MISMATCH`  |
| ICEGATE LEO present and GRANTED         | `LEO_MISSING`      |

A controlled slice of the dataset deliberately mismatches (e-seal tamper,
weighbridge vs Form-13 weight discrepancy, missing ICEGATE LEO) so the flags
fire. `leo.customs_alerts(result)` shapes those flags as `jnpa_shared` `Alert`
dicts (`kind="CUSTOMS_FLAG"`) for the dashboard's Customs feed.

## Metrics

| Metric                       | Labels            |
| ---------------------------- | ----------------- |
| `leo_reconciliations_total`  | `result=ready\|blocked` |
| `customs_flags_total`        | `flag`            |

## Verify

```bash
curl -s http://localhost:8350/healthz | jq .
curl -s http://localhost:8350/records/MSCU1234567 | jq .
curl -s -X POST http://localhost:8350/leo -H 'content-type: application/json' \
     -d '{"container_no":"MAEU7654321"}' | jq .
curl -s http://localhost:8350/customs/flags | jq '.by_flag'
```
