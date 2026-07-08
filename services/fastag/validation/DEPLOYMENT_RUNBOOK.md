# FASTag Module — Deployment & Verification Runbook (Step 5.5)

Steps to deploy the FASTag module (part of the gateway image) to the target
environment and verify it starts without manual intervention. Commands assume the
repo root and the AWS compose overlay already used by this project.

---

## 0. Prerequisites
- `.env.prod` (or your env file) populated from `.env.aws.example`, including the
  **FASTag vendor block** (see `CONTRACT_VERIFICATION.md`):
  `FASTAG_ULIP_URL`, `FASTAG_ULIP_*_PATH`, `FASTAG_ULIP_AUTH_SCHEME`,
  `FASTAG_ULIP_AUTH_HEADER`, `ULIP_API_KEY`.
- `APP_ENV=staging` or `production` → forces the auth fail-fast guard
  (`AUTH_ENABLED=true`, non-default `AUTH_JWT_SECRET`, `AUTH_DEV_TOKENS=false`).

## 1. Database migration (existing DB — do this BEFORE deploy)
`init.sql` only runs on a fresh volume, so apply the migration to the live DB:
```bash
# libpq DSN = the app's POSTGRES_DSN with "+asyncpg" stripped.
psql "postgresql://<user>:<pw>@<host>:5432/<db>" -v ON_ERROR_STOP=1 \
     -f infra/postgres/migrations/0001_fastag.sql
# verify:
psql "...same DSN..." -c "\dt jnpa.fastag_balance" \
     -c "\dt jnpa.fastag_transactions" -c "\dt jnpa.toll_enroute"
```
Idempotent — safe to re-run.

## 2. Build (verifies packaging — Blocker B1)
```bash
docker compose build gateway
```
The build's import guard runs
`python -c "import services.fastag; from services.fastag import UlipFastagClient, FastagService"`
— **if `services/` were missing from the image the build fails here.** A green build
proves the packaging fix.

## 3. Deploy
```bash
# base + AWS overlay (the overlay inherits the gateway env/volumes from base)
docker compose -f docker-compose.yml -f docker-compose.aws.yml --env-file .env.prod up -d gateway
```

## 4. Startup validation
```bash
docker compose logs -f gateway        # expect: "gateway_starting", no traceback
# container-level health (Docker HEALTHCHECK hits /healthz):
docker inspect --format '{{.State.Health.Status}}' jnpa-gateway   # -> healthy
```

## 5. FASTag module health
```bash
# through the gateway (add Authorization: Bearer <token> if AUTH_ENABLED):
curl -s http://<host>:8000/api/fastag/health | jq
# expect: {"module":"fastag","status":"ok","ulip_configured":true,
#          "db":"ok","tables":{"fastag_balance":true,...}}
```
`ulip_configured:false` → `FASTAG_ULIP_URL` not loaded. `db:"unreachable"` → DSN/network.

## 6. Live vendor validation (Step 5.3)
Run the harness from a host that can reach the provider:
```bash
PYTHONPATH="shared:." python -m services.fastag.validation.live_validation \
    --valid-rc <REAL_ENROLLED_RC> --unknown-rc <WELL_FORMED_UNKNOWN_RC> \
    --report ./fastag_live_report.json
```
Attach `fastag_live_report.json` to the release record. Drive the PENDING scenarios
(empty/partial/4xx/5xx) via the provider sandbox or a fault-injection proxy.

## 7. Restart behaviour
```bash
docker compose restart gateway
# then re-check step 4 + 5. Compose restart policy is `unless-stopped`, so the
# container also survives an EC2 reboot; confirm with:
sudo reboot   # (on the EC2 host) then re-run steps 4–5 after it comes back.
```

## 8. Environment loading check
```bash
docker compose exec gateway printenv | grep -E 'FASTAG_ULIP|ULIP_API_KEY|APP_ENV|AUTH_ENABLED'
# ULIP_API_KEY should be present in-process but NEVER appears in logs (redacted).
```

---

## Rollback
FASTag is additive: routes under `/api/fastag/*`, three new tables, a new package.
To disable without redeploying code, unset `FASTAG_ULIP_URL` (calls return a clean
500 `config` error; the rest of the gateway is unaffected). The tables and migration
are inert if unused.

## Sign-off gates (all must pass for READY)
- [ ] Migration applied + tables verified on the live DB
- [ ] `docker compose build gateway` green (packaging guard passed)
- [ ] `/healthz` healthy, `/api/fastag/health` status=ok
- [ ] `fastag_live_report.json`: success + auth-failure + timeout/retry confirmed
- [ ] Contract checklist complete; `bank_name`/`status` decision recorded
- [ ] Restart + EC2-reboot survival confirmed
- [ ] No secrets in logs (`printenv` shows key; logs do not)
