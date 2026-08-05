# identity — face-recognition driver verification (Appendix C #2)

A FastAPI service (port **8360**) that demonstrates the **PDP-augmentation**
verification pipeline for the JNPA UC-III Digital Twin: a driver's live face
capture is matched against an enrolled gallery, and the result feeds the same
admit / **PROVISIONAL** / reject machinery the Vahan fallback uses at the gate.

## DPDP posture — synthetic, consented biometrics only

> **No real driver biometrics are processed.** The enrolled gallery and every
> "live capture" are **synthetic and consented**. Given DPDP Act exposure
> (bid §7.4), real PDP biometric enrollment is a **post-award, consent-gated**
> workflow. This PoC proves the *mechanism* (embed → match → PROVISIONAL on
> miss) without handling personal data — see
> [`docs/ASSUMPTIONS.md`](../docs/ASSUMPTIONS.md) *"Identity / face-recognition
> (C2)"*.

The embedding stage is **simulated**: a production system runs a CNN such as
**ArcFace** over a camera frame to emit a ~512-d template; here `embeddings.py`
derives a deterministic unit-norm vector from a hash of the `driver_id`, so the
match / threshold / PROVISIONAL logic is provable with zero biometric data and
is identical across runs, hosts, and CI. Swapping the embedder for a real CNN
leaves the decision logic unchanged.

## Endpoints

| Method | Path          | Returns                                            |
| ------ | ------------- | -------------------------------------------------- |
| GET    | `/healthz`    | `{status, service, enrolled, synthetic: true}`     |
| GET    | `/gallery`    | enrolled drivers (ids/names/licences; no templates)|
| GET    | `/threshold`  | configured match / provisional thresholds          |
| POST   | `/verify`     | `VERIFIED` \| `PROVISIONAL` \| `REJECTED`          |
| GET    | `/metrics`    | Prometheus exposition                              |

### `POST /verify`

Request body:

```json
{ "driver_id": "DRV-0001", "claimed": true, "simulate": "genuine" }
```

`simulate` is a PoC affordance (a real service reads the camera frame):
`genuine` = the enrolled driver presents, `impostor` = someone else,
`unknown` = an impostor-style mismatch.

Response:

```json
{
  "driver_id": "DRV-0001",
  "matched": true,
  "score": 0.9713,
  "decision": "VERIFIED",
  "provisional_until": null,
  "cure_window_h": 24,
  "reason": "face_match",
  "synthetic": true
}
```

## Decision logic

A live capture embedding is matched (cosine similarity) against the claimed
enrollment:

| Condition                                                  | Decision      |
| ---------------------------------------------------------- | ------------- |
| `score >= 0.9`                                             | `VERIFIED`    |
| `0.5 <= score < 0.9`, **or** an unknown/unenrolled driver  | `PROVISIONAL` |
| `score < 0.5`                                              | `REJECTED`    |

A **PROVISIONAL** decision mirrors the Vahan `admit_provisional` path
([`gateway/provisional.py`](../gateway/provisional.py)): the driver is admitted
**on trust** with a 24h **cure window** (`provisional_until`), pending manual
verification before it closes — so a face-match miss never blocks port
operations, it just raises a leash. Thresholds and the cure window are tunable
via `IDENTITY_VERIFY_THRESHOLD`, `IDENTITY_PROVISIONAL_THRESHOLD`, and
`IDENTITY_CURE_WINDOW_H`.

## Deterministic gallery

`gallery.py` builds **50** synthetic enrolled drivers (`DRV-0001` …) with
deterministic names, DL-style licence numbers, and enrollment embeddings derived
from a fixed `SEED`. Every record is flagged `synthetic` + `consented`. Size is
tunable via `IDENTITY_GALLERY_SIZE`.

## Observability

`identity_verifications_total{decision}` counts attempts by outcome so the
dashboard shows the match-rate and the PROVISIONAL (admit-on-trust) rate side by
side.

## Verify

```bash
curl -s http://localhost:8360/healthz | jq .
curl -s -X POST http://localhost:8360/verify \
  -H 'content-type: application/json' \
  -d '{"driver_id":"DRV-0001","simulate":"genuine"}' | jq .
curl -s http://localhost:8360/threshold | jq .
```
