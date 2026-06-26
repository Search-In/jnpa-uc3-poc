# Driver Enrolment & Identity Verification — Production Runbook

This subsystem runs in two modes, selected by **`APP_ENV`** (same dev/prod
classification as the auth layer). The mode decides whether resilience fallbacks
are allowed.

## Modes

| Capability            | DEV (`APP_ENV=development`, default) | PRODUCTION (`APP_ENV=production`) |
|-----------------------|--------------------------------------|----------------------------------|
| Enrolment store       | Postgres, **in-memory fallback** if DB down | **Postgres REQUIRED** — DB down → structured `503` (no memory) |
| Face image storage    | MinIO, **base64 fallback** in DB     | **MinIO REQUIRED** — upload fail → `503` (no base64) |
| Identity matching     | ArcFace ONNX, **synthetic fallback** (no-image/simulate) | **ArcFace ONNX REQUIRED** — service down on approval → `503`; a real capture is never synthetic-passed |
| Auth / RBAC           | off (frictionless)                   | **REQUIRED** (`validate_auth_config` fails startup otherwise) |

Mode is logged at startup: `gateway_runtime_mode mode=...`.
A real captured frame is **never** matched by the synthetic (driver-id-keyed)
path in any mode — if the model is unreachable the result is `PROVISIONAL`
(`reason=identity_service_unavailable`), never a false `VERIFIED`.

## Data model (3 tables, `jnpa` schema)

- **`drivers`** — canonical master identity (promoted on approval). Verification
  reads the active driver here. Holds profile + MinIO `photo_url` + template
  metadata. Embeddings live in the identity service, not here.
- **`driver_enrollments`** — the workflow/request record (`PENDING → ACTIVE →
  REJECTED → REENROLL`) + captured frames pending review.
- **`verification_logs`** — append-only audit of every `/verify` decision
  (driver, decision, score, provider, path, actor, purpose, reason, ts).
- `enrollment_audit` — append-only enrolment lifecycle audit (DPDP).

## API (all via the gateway, DPDP-audited)

| Method | Path | Role | Purpose |
|--------|------|------|---------|
| POST | `/api/identity/enrol-request` | DRIVER + admin | PWA submits profile + consented frames → PENDING |
| GET  | `/api/identity/enrol-request/{driver_id}` | DRIVER + admin | driver polls own status |
| GET  | `/api/identity/enrollments[?status=]` | CUSTOMS/ADMIN | admin queue |
| GET  | `/api/identity/enrollments/{driver_id}` | CUSTOMS/ADMIN | review (incl. frames) |
| POST | `/api/identity/enrollments/{driver_id}/approve` | CUSTOMS/ADMIN | mint template + MinIO photo + promote to `drivers` |
| POST | `/api/identity/enrollments/{driver_id}/reject` | CUSTOMS/ADMIN | reject (driver may resubmit) |
| POST | `/api/identity/enrollments/{driver_id}/reenroll` | CUSTOMS/ADMIN | request re-capture |
| POST | `/api/identity/verify` | CUSTOMS/ADMIN | 1:1 ArcFace match vs the selected driver's template |
| POST | `/api/identity/identify` | CUSTOMS/ADMIN | **1:N** — captured face → nearest enrolled driver (no selection) |
| GET  | `/api/identity/drivers` | CUSTOMS/ADMIN | active master drivers |
| GET  | `/api/identity/verifications[?driver_id=]` | CUSTOMS/ADMIN | verification audit trail |

### Full pipeline (1:N `/identify`)

```
Camera frame → Face detection → Quality gate (STRICT) → Liveness (STRICT when
model present) → ArcFace embedding → cosine nearest-neighbour over jnpa.driver_faces
→ top-1 ≥ threshold ? driver_id : UNKNOWN → verification_logs (Postgres)
```
`/identify` returns `{decision, driver_id, candidate_id, score, gallery_size,
quality, liveness}`. A gate failure (`quality:*` / `liveness:spoof_detected`)
stops the pipeline before matching. Threshold `_IDENTIFY_THRESHOLD` (0.45 ArcFace
cosine); pgvector isn't in the DB image so the search is in-app cosine over
`jnpa.driver_faces` (swap to pgvector/FAISS for large fleets).

## Required production env

```bash
APP_ENV=production
ALLOW_FALLBACK=false                          # belt-and-braces (prod forces this anyway)
# Auth (enforced at startup)
AUTH_ENABLED=true
AUTH_JWT_SECRET=$(openssl rand -hex 32)
AUTH_DEV_TOKENS=false
PWA_PAIRING_SECRET=<shared-secret>          # PWA DRIVER token minting
# Persistence
POSTGRES_DSN=postgresql+asyncpg://<user>:<pw>@postgres:5432/<db>
# Object storage (identity reference photos -> bucket "drivers")
MINIO_ENDPOINT=minio:9000
MINIO_ACCESS_KEY=<key>
MINIO_SECRET_KEY=<secret>
MINIO_SECURE=true                            # behind TLS
DRIVER_ENROL_BUCKET=drivers
DRIVER_ENROL_URL_BASE=https://<minio-public>/drivers
# Real face recognition (identity service)
IDENTITY_EMBEDDER=onnx
IDENTITY_ARCFACE_MODEL=/models/arcface.onnx  # fetch: scripts/fetch_face_model.sh
IDENTITY_VERIFY_THRESHOLD=0.45               # tuned for ArcFace cosine
IDENTITY_PROVISIONAL_THRESHOLD=0.30
# Liveness / anti-spoofing (REQUIRED for real security)
IDENTITY_LIVENESS=true
IDENTITY_LIVENESS_MODEL=/models/antispoof.onnx   # fetch: scripts/fetch_face_model.sh
IDENTITY_LIVENESS_THRESHOLD=0.5
IDENTITY_LIVENESS_REAL_INDEX=0               # hairymax AntiSpoofing_bin: 0=real
```

Both models are fetched by `scripts/fetch_face_model.sh` into `data/models/`
(git-ignored) and mounted at `/models`.

The dashboard build must be live: `VITE_DATA_MODE=live`.

## Pre-deployment checklist (validated)

1. Enrol a driver from the PWA → `PENDING`.
2. Approve in the admin portal → `ACTIVE`, promoted to `drivers`, template minted, photo in MinIO.
3. Restart gateway + identity.
4. Driver still `ACTIVE` (read from Postgres, not memory).
5. Verify with the correct live face → `VERIFIED` (template self-healed from the persisted reference).
6. Verify with a different face → `REJECTED`.
7. `verification_logs` and `enrollment_audit` populated.

## Biometric quality + liveness gates

Run on real captures in the ONNX pipeline (skipped under the synthetic provider):

- **Quality (active, no model)** — exactly one face, sharpness (Laplacian var),
  brightness, and face-size are checked on `/verify` and `/enrol`. A failing frame
  returns `decision=REJECTED, reason=quality:<code>` (verify) or refuses enrolment
  (`enrolled=false`; admin approval returns `422 reference_quality_failed`). Codes:
  `no_face_detected | multiple_faces | face_too_small | image_too_blurry |
  too_dark | too_bright`. Tunable via `IDENTITY_QUALITY_*` env.
- **Liveness / anti-spoofing (pluggable)** — set `IDENTITY_LIVENESS=true` and mount
  a MiniFASNet-style anti-spoof model at `IDENTITY_LIVENESS_MODEL` (80×80 input).
  When present it is authoritative: a spoof returns `REJECTED, reason=liveness:spoof_detected`.
  Without a model it degrades to a logged passive advisory that never hard-rejects.
  Responses carry `quality` and `liveness` blocks for the UI / audit.

## Known gaps (remaining before full enterprise-grade)

- **Liveness is single-frame passive** — a real anti-spoof model is integrated and
  enforced (`IDENTITY_LIVENESS=true`), validated to score real faces live and flat
  images as spoof, but its efficacy against print/replay attacks must be validated
  with real attack samples on your hardware, and active challenge-response
  (blink/turn) is stronger. Tune `IDENTITY_LIVENESS_THRESHOLD` against your data.
- **Face alignment is Haar bbox crop** (no 5-point landmark alignment) — genuine
  ArcFace scores ~0.6 vs ~0.85 with alignment; thresholds are set accordingly.
- **1:N search is in-app cosine** over `jnpa.driver_faces` (no pgvector in the DB
  image) — fine for thousands of drivers; use pgvector/FAISS for larger fleets.
- No duplicate-face detection at enrolment (same face under two driver IDs).
- Single identity instance, CPU inference, no GPU/HA/model-rotation.
