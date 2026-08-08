# QA Deployment — JNPA UC-III PoC

How to run a **QA** stack of this repository on the **same EC2 box as production**,
against a **separate RDS database**, on **separate ports**, with **separate
container names**, without stopping, rebuilding or reconfiguring anything
production owns.

Branch: `qa` · Compose project: `jnpa-uc3-poc-qa` · Database: `jnpa_qa` · Checkout: `~/jnpa-uc3-poc-qa`

| | Production | QA |
|---|---|---|
| Compose project | `jnpa-uc3-poc` | `jnpa-uc3-poc-qa` |
| Compose files | `docker-compose.yml` + `docker-compose.aws.yml` | the same two, **+ `docker-compose.qa.yml` last** |
| Env file | `.env` (from `.env.aws.example`) | `.env.qa` (from `.env.qa.example`) |
| Database | `jnpa_schema_v3` | `jnpa_qa` |
| Container names | `jnpa-*` | `jnpa-qa-*` |
| Host ports | 80, 443, 3000, 8000, 8210 | **18000→8000** (gateway), **18080→3000** (web) |
| Built image tags | `jnpa/<name>:0.1.0` | `jnpa/<name>:qa` |
| Checkout on EC2 | `/home/ec2-user/jnpa-uc3-poc` | `/home/ec2-user/jnpa-uc3-poc-qa` |
| URL | https://traffic-three.searchintech.in/ | `http://<EC2_PUBLIC_HOST>:18080/` (TLS hostname later — §9/§10) |

Nothing in this document modifies production. The only file shared between the
two stacks is `docker-compose.yml`, and QA reads it from its **own checkout**.

---

## 1. QA database configuration

The QA database already exists on the **same RDS instance** as production:

| Field | Value |
|---|---|
| Host | `__RDS_HOST__` — the **same endpoint production already uses**. Deliberately not committed (`docs/RDS_SECURITY.md`, enforced by `tests/test_rds_security.py`); read it from production's `.env` on the box: `grep -m1 POSTGRES_DSN /home/ec2-user/jnpa-uc3-poc/.env` |
| Port | `5432` |
| Database | `jnpa_qa` |
| User | `jnpa_app` (least-privilege application role — `docs/RDS_SECURITY.md` §3) |
| Password | **never in Git** — set on EC2 only |
| SSL | required (`ssl=require` for asyncpg, `sslmode=require` for libpq) |

The schema/tables are already present in `jnpa_qa`; this deployment does not
create, migrate or drop anything.

**The database name is not hard-coded anywhere in application code.** Every
service reads a DSN from the environment (`shared/jnpa_shared/db.py` and the
`${VAR:?…}` declarations in `docker-compose.yml`), exactly as production does.
Switching QA to a different database is a change to `.env.qa` only.

Two DSN dialects are required and **must not be normalised to one**:

* `POSTGRES_DSN` — SQLAlchemy async engine over **asyncpg**, which accepts
  `?ssl=require`.
* `RFID_/TRUCK_/CONGESTION_/ANOMALY_POSTGRES_DSN` — **libpq/psycopg**, which
  needs `?sslmode=require`.

---

## 2. Required QA environment variables

The repository's convention is **DSN-based** (`POSTGRES_DSN` /
`*_POSTGRES_DSN`), not `POSTGRES_HOST`/`POSTGRES_DB` — `.env.qa.example` keeps
that convention. The QA-specific values are:

```dotenv
APP_ENV=qa

POSTGRES_PASSWORD=<SET_ON_EC2_ONLY>

# asyncpg / SQLAlchemy async engine (gateway + the other Python services)
POSTGRES_DSN=postgresql+asyncpg://jnpa_app:<SET_ON_EC2_ONLY>@__RDS_HOST__:5432/jnpa_qa?ssl=require

# libpq / psycopg DSNs (surface to their services as POSTGRES_DSN_LIBPQ)
RFID_POSTGRES_DSN=postgresql://jnpa_app:<SET_ON_EC2_ONLY>@__RDS_HOST__:5432/jnpa_qa?sslmode=require
TRUCK_POSTGRES_DSN=postgresql://jnpa_app:<SET_ON_EC2_ONLY>@__RDS_HOST__:5432/jnpa_qa?sslmode=require
CONGESTION_POSTGRES_DSN=postgresql://jnpa_app:<SET_ON_EC2_ONLY>@__RDS_HOST__:5432/jnpa_qa?sslmode=require
ANOMALY_POSTGRES_DSN=postgresql://jnpa_app:<SET_ON_EC2_ONLY>@__RDS_HOST__:5432/jnpa_qa?sslmode=require

# Grafana's Postgres datasource — same RDS, QA database
GRAFANA_PG_HOST=__RDS_HOST__:5432
GRAFANA_PG_DB=jnpa_qa
GRAFANA_PG_USER=jnpa_app
GRAFANA_PG_SSLMODE=require
# (the datasource password is read from POSTGRES_PASSWORD — GRAFANA_PG_PASSWORD has no effect)

# QA is plain HTTP on 18080 until it has its own TLS hostname (§9)
MINIO_PUBLIC_ENDPOINT=__QA_HOST__:18080/minio
MINIO_PUBLIC_SECURE=false
```

Secrets that must be generated on the box (every `<SET_ON_EC2_ONLY>` in
`.env.qa.example`): `POSTGRES_PASSWORD`, `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`,
`GRAFANA_ADMIN_PASSWORD`, `AUTH_JWT_SECRET`, `INTERNAL_SERVICE_TOKEN`,
`PWA_PAIRING_SECRET` / `VITE_PWA_PAIRING_SECRET`.

**`APP_ENV=qa` is production-like** — `gateway/auth.py` classifies anything
outside `development|dev|local|test` as production, so QA must also run
`AUTH_ENABLED=true`, a non-default `AUTH_JWT_SECRET`, and `AUTH_DEV_TOKENS=false`
or the gateway refuses to start. `.env.qa.example` already sets these. The same
classification makes `identity` require the real ONNX models (§3, step 4).

---

## 3. How to create the QA `.env` on EC2

QA lives in its **own checkout** so the production deploy's
`rsync --delete` into `/home/ec2-user/jnpa-uc3-poc` can never touch it.

```bash
# 1. Clone the qa branch into a separate directory (once)
cd /home/ec2-user
git clone -b qa https://github.com/Search-In/jnpa-uc3-poc.git jnpa-uc3-poc-qa
cd /home/ec2-user/jnpa-uc3-poc-qa

# 2. Create the QA env file from the template
cp .env.qa.example .env.qa
chmod 600 .env.qa

# 3. Put the RDS endpoint in the shell (it is not committed — docs/RDS_SECURITY.md).
#    Read it out of production's env file, or copy it from the RDS console.
export RDS_ENDPOINT="$(grep -m1 '^POSTGRES_DSN=' /home/ec2-user/jnpa-uc3-poc/.env \
                        | sed -E 's#.*@([^:/]+):.*#\1#')"
echo "$RDS_ENDPOINT"        # sanity-check before continuing

# 4. Write the RDS/jnpa_qa DSNs. Reuse the EXISTING writer — it rewrites only the
#    six Postgres lines and leaves everything else untouched.
RDS_HOST="$RDS_ENDPOINT" \
RDS_DB=jnpa_qa \
RDS_USER=jnpa_app \
RDS_PASSWORD='<the real QA password>' \
  bash deploy/aws/use-rds.sh .env.qa

# 5. Fill the remaining <SET_ON_EC2_ONLY> secrets
sed -i "s|MINIO_ACCESS_KEY=<SET_ON_EC2_ONLY>|MINIO_ACCESS_KEY=$(openssl rand -hex 16)|"      .env.qa
sed -i "s|MINIO_SECRET_KEY=<SET_ON_EC2_ONLY>|MINIO_SECRET_KEY=$(openssl rand -hex 24)|"      .env.qa
sed -i "s|GRAFANA_ADMIN_PASSWORD=<SET_ON_EC2_ONLY>|GRAFANA_ADMIN_PASSWORD=$(openssl rand -hex 16)|" .env.qa
sed -i "s|AUTH_JWT_SECRET=<SET_ON_EC2_ONLY>|AUTH_JWT_SECRET=$(openssl rand -hex 32)|"        .env.qa
sed -i "s|INTERNAL_SERVICE_TOKEN=<SET_ON_EC2_ONLY>|INTERNAL_SERVICE_TOKEN=$(openssl rand -hex 24)|"  .env.qa
PAIR="$(openssl rand -hex 24)"
sed -i "s|PWA_PAIRING_SECRET=<SET_ON_EC2_ONLY>|PWA_PAIRING_SECRET=${PAIR}|"                  .env.qa
sed -i "s|VITE_PWA_PAIRING_SECRET=<SET_ON_EC2_ONLY>|VITE_PWA_PAIRING_SECRET=${PAIR}|"        .env.qa

# 6. Point MinIO's presigned-URL host at the QA origin
sed -i "s|__QA_HOST__|$(curl -s http://169.254.169.254/latest/meta-data/public-hostname)|" .env.qa

# 7. Nothing must remain unset
grep -n 'SET_ON_EC2_ONLY\|__QA_HOST__' .env.qa && echo "^^ still unfilled" || echo "all placeholders replaced"
```

`.env.qa` is git-ignored (`.gitignore`), so it can never be committed. The
template `.env.qa.example` stays tracked.

**Two prerequisites before the first `up`:**

1. **Frontend bundles.** `web/Dockerfile` copies pre-built `web/dist` and
   `mobile-pwa/dist` (it does not compile them) and *refuses to build* unless the
   dashboard bundle is in LIVE mode. Build them in the QA checkout:
   ```bash
   pnpm install --frozen-lockfile=false
   NODE_OPTIONS="--max-old-space-size=8192 --max-semi-space-size=512" \
     VITE_DATA_MODE=live VITE_AUTH_ENABLED=true \
     VITE_PWA_PAIRING_SECRET="$(grep '^PWA_PAIRING_SECRET=' .env.qa | cut -d= -f2-)" \
     pnpm -r --workspace-concurrency=1 build
   bash scripts/verify_web_live_build.sh web/dist
   ```
   Build one workspace at a time (`--workspace-concurrency=1`) — parallel Vite
   processes OOM the instance. If the box is memory-tight, build on a laptop/CI
   and `rsync` `web/dist` + `mobile-pwa/dist` up instead.
2. **Identity models.** `APP_ENV=qa` is production-like, so `identity` crash-loops
   without ArcFace + anti-spoof ONNX models:
   ```bash
   bash scripts/fetch_face_model.sh      # writes ./data/models in the QA checkout
   ```

---

## 4. How to start only QA containers

**Validate first — never start QA on an unvalidated config.** This is the exact
render the whole design depends on:

```bash
cd /home/ec2-user/jnpa-uc3-poc-qa

docker compose --env-file .env.qa -p jnpa-uc3-poc-qa \
  -f docker-compose.yml -f docker-compose.aws.yml -f docker-compose.qa.yml \
  config > /tmp/qa-config.yml && echo "config OK"

# Must print ONLY 18000 and 18080
grep 'published:' /tmp/qa-config.yml

# Must print nothing (every container renamed)
grep 'container_name:' /tmp/qa-config.yml | grep -v 'jnpa-qa-'

# Must print .../jnpa_qa and nothing else
grep -oE '@[A-Za-z0-9._-]+:5432/[a-z_0-9]+' /tmp/qa-config.yml | sort -u

# Must print APP_ENV: qa
grep 'APP_ENV:' /tmp/qa-config.yml | sort -u

# Nothing QA wants may already be bound on the host
sudo ss -ltnp | grep -E ':(18000|18080)\b' || echo "18000/18080 free"
```

Only when all five are clean:

```bash
./deploy/qa/manage.sh up
```

which is exactly:

```bash
docker compose --env-file .env.qa -p jnpa-uc3-poc-qa \
  -f docker-compose.yml -f docker-compose.aws.yml -f docker-compose.qa.yml \
  up -d --build
```

Every flag matters:

* `-p jnpa-uc3-poc-qa` — separate compose project ⇒ separate network (`jnpa-uc3-poc-qa_jnpa`)
  and separate named volumes (`jnpa-uc3-poc-qa_pgdata`, `jnpa-uc3-poc-qa_minio-data`, …).
* `-f docker-compose.qa.yml` **last** — the QA overlay: `jnpa-qa-*` container
  names, `jnpa/*:qa` image tags, every host port reset except 18000/18080.
  `docker-compose.aws.yml` (applied before it) gives every service
  `restart: unless-stopped`, publishes web on 80/443 and mounts
  `/etc/letsencrypt`; the QA overlay's `!override` entries **replace** the last
  two. But `!override` only replaces values from files applied **earlier** —
  reorder the flags and QA would inherit production's ports and its TLS keys.
  `deploy/qa/manage.sh` always gets the order right.
* `--env-file .env.qa` — RDS `jnpa_qa`.

`manage.sh` refuses to start if `.env.qa` contains a `jnpa_schema_v3` DSN.

The first `up` builds ~20 images and takes a while; later runs are incremental.

---

### Why QA runs its own Redis / Kafka / MQTT / MinIO

They are **isolated, not shared**. Production's copies hold live state and, under
`docker-compose.aws.yml`, publish no host ports at all — they are reachable only
from inside production's own bridge network. Sharing them would mean attaching QA
to that network (which defeats every isolation guarantee in §11) and letting QA
writes land in production's event streams, caches and object buckets. QA
therefore starts its own on the `jnpa-uc3-poc-qa_jnpa` network with no host ports
and `jnpa-qa-*` names. The cost is RAM on the instance; check `free -m` and
`df -h /` before the first `up`.

---

## 5. How to stop / restart only QA containers

```bash
cd /home/ec2-user/jnpa-uc3-poc-qa

./deploy/qa/manage.sh ps          # QA status only
./deploy/qa/manage.sh logs        # follow QA gateway + web
./deploy/qa/manage.sh restart     # restart QA services
./deploy/qa/manage.sh down        # stop + remove QA containers (volumes kept)
./deploy/qa/manage.sh update      # git pull + rebuild changed QA images + up

# single QA service
docker compose --env-file .env.qa -p jnpa-uc3-poc-qa \
  -f docker-compose.yml -f docker-compose.aws.yml -f docker-compose.qa.yml \
  restart gateway
docker restart jnpa-qa-gateway    # equivalent, by container name
```

Every one of these is scoped to the `jnpa-uc3-poc-qa` project or to a `jnpa-qa-*`
container name, so none of them can reach a production container.

**Never run these from the QA checkout** (they are unscoped and would hit
production): bare `docker compose down`, `docker system prune`,
`docker stop $(docker ps -q)`. `deploy/qa/manage.sh` deliberately has no `nuke`.

---

## 6. How to verify QA database connectivity

One command checks the env file, the rendered config, the ports, the running
containers, a live query, **and** that production is still on `jnpa_schema_v3`:

```bash
cd /home/ec2-user/jnpa-uc3-poc-qa
bash deploy/qa/verify-qa.sh          # expects RESULT: QA -> jnpa_qa, production intact ✔
```

Manual equivalents:

```bash
# DSNs in the env file (password masked)
grep -E '^(POSTGRES_DSN|RFID_|TRUCK_|CONGESTION_|ANOMALY_)' .env.qa | sed -E 's/:[^:@/]*@/:****@/'

# The QA gateway's own connection — must print db=jnpa_qa and ssl=True
docker exec jnpa-qa-gateway python -c "
import asyncio, os
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
async def m():
    e = create_async_engine(os.environ['POSTGRES_DSN'])
    async with e.connect() as c:
        print(dict((await c.execute(text(
            'SELECT current_database() db, inet_server_addr()::text server,'
            ' (SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()) ssl'))).mappings().one()))
    await e.dispose()
asyncio.run(m())"

# psql from the box (read-only sanity check; never run DDL/DML against QA data)
psql "postgresql://postgres@__RDS_HOST__:5432/jnpa_qa?sslmode=require" \
  -c "select current_database(), count(*) from information_schema.tables where table_schema not in ('pg_catalog','information_schema');"
```

Do **not** use `deploy/aws/verify-rds.sh` for QA: its container filter is the
substring `jnpa-`, which also matches `jnpa-qa-*`, so it reports QA containers as
NOT-RDS. It stays correct for production as long as you read past those lines —
`deploy/qa/verify-qa.sh` filters by the exact compose project label instead.

---

## 7. How to verify the QA gateway

```bash
curl -fsS http://127.0.0.1:18000/healthz            # from the EC2 box
curl -fsS http://<EC2_PUBLIC_HOST>:18000/healthz    # from outside (needs the SG rule below)
curl -fsS http://127.0.0.1:18000/openapi.json | head -c 200
docker logs --tail 100 jnpa-qa-gateway
docker inspect -f '{{.State.Health.Status}}' jnpa-qa-gateway   # -> healthy
```

Authenticated routes return 401 without a bearer token — that is correct, QA runs
`AUTH_ENABLED=true`.

To reach 18000/18080 from outside, add **inbound** rules for TCP 18000 and 18080
to the EC2 security group, scoped to your office/VPN CIDR (not `0.0.0.0/0`).
Leaving them closed is fine if you only test through an SSH tunnel:

```bash
ssh -L 18080:localhost:18080 -L 18000:localhost:18000 ec2-user@<EC2_HOST>
```

---

## 8. How to verify QA web

```bash
curl -I http://127.0.0.1:18080                                              # 200 OK
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18080/          # 200
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18080/pwa/      # 200 (driver PWA)
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18080/api/healthz  # 200 via the QA nginx -> QA gateway
docker inspect -f '{{.State.Health.Status}}' jnpa-qa-web                    # -> healthy
```

The QA healthcheck probes with BusyBox `wget`. (Production's baked healthcheck in
`web/Dockerfile` uses `curl`, which the `nginx:alpine` base image does not ship —
so `jnpa-web` can report `unhealthy` while serving perfectly. Nothing depends on
that container's health condition, which is why it has gone unnoticed; it is left
alone here because fixing it means rebuilding the production image.)

Then open `http://<EC2_PUBLIC_HOST>:18080/` in a browser (or `http://localhost:18080/`
through the SSH tunnel).

The QA web container serves `web/nginx/default.qa.conf` — plain HTTP on container
port 3000, mounted over the baked production config (which listens on 80/443 and
loads production's Let's Encrypt certificate). Its `/api`, `/api/ws`, `/poc3/`,
`/minio/`, `/assets/` and `/pwa/` blocks are identical to production's.

---

## 9. Future Nginx / SSL configuration

**Nothing here is done yet — production Nginx, DNS and certificates are untouched.**

Note the ambiguity to settle first: `traffic-three.searchintech.in` is the
**production** hostname today. "QA public URL target: traffic-three…" can mean
two very different things, and they need different work:

### Option A — QA gets its own hostname (recommended, zero production risk)

e.g. `qa-traffic-three.searchintech.in`. Because production's web container owns
80/443, put a host-level Nginx (or an ALB with host-based routing) in front and
proxy the QA name to `127.0.0.1:18080`:

```nginx
# NEW FILE on the host, e.g. /etc/nginx/conf.d/qa.conf — do NOT edit production's config
server {
    listen 80;
    server_name qa-traffic-three.searchintech.in;
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 301 https://$host$request_uri; }
}
server {
    listen 443 ssl;
    server_name qa-traffic-three.searchintech.in;

    ssl_certificate     /etc/letsencrypt/live/qa-traffic-three.searchintech.in/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/qa-traffic-three.searchintech.in/privkey.pem;

    client_max_body_size 25m;

    location /api/ws {                       # WebSocket — must precede /api/
        proxy_pass http://127.0.0.1:18080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 3600s;
    }
    location / {
        proxy_pass http://127.0.0.1:18080;   # QA web container
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 60s;
    }
}
```

**Blocker to plan for:** a host Nginx cannot bind 80/443 while production's `web`
container holds them (`docker ps --filter publish=443` names the owner). Either
run the QA front-end on different ports, or schedule a short production change
that moves its container behind the same host Nginx. That production change is
explicitly out of scope here.

Alternative without a host Nginx: give the QA `web` container its own TLS
listener — add the two server blocks from `web/nginx/default.conf` to
`web/nginx/default.qa.conf` with the QA `server_name`/cert paths, publish
`"18443:443"` in `docker-compose.qa.yml`, and mount `/etc/letsencrypt:ro`. The
QA URL then carries an explicit port (`https://qa-…:18443/`).

### Option B — `traffic-three.searchintech.in` is eventually pointed AT QA (a cutover)

This is a production change, not a QA one. It is not performed now. When it is
scheduled, the sequence is: bring QA to parity and verify it on Option A's
hostname → decide whether QA *replaces* production or the two swap → move the
80/443 binding (only one container can hold them) → re-point the certificate and
`MINIO_PUBLIC_ENDPOINT`/`CORS_ALLOW_ORIGINS` → keep the previous stack running on
its own port for rollback. Until that is agreed, QA stays on
`http://<EC2_PUBLIC_HOST>:18080/`.

After **either** option, update `.env.qa` and restart only QA:

```dotenv
MINIO_PUBLIC_ENDPOINT=<qa-hostname>/minio
MINIO_PUBLIC_SECURE=true
CORS_ALLOW_ORIGINS=https://<qa-hostname>
```

Constraints, either way: do not add QA `server_name`s to production's server
blocks, do not re-point or reuse production's certificate, and do not bind
anything new to 80/443 while the production web container owns them.

---

## 10. Future DNS / Route 53 configuration

**Not created yet, by design (requirement: do not alter DNS).** When ready:

1. Hosted zone `searchintech.in` → **Create record**.
2. Name `qa-traffic-three`, type **A**, value = the EC2 **public IP** (or an
   **A / Alias** to the ALB if Option B uses one), TTL 300.
3. Confirm propagation: `dig +short qa-traffic-three.searchintech.in`.
4. Only then issue the certificate (HTTP-01 needs the name to resolve first):
   ```bash
   sudo certbot certonly --webroot -w /var/www/certbot \
     -d qa-traffic-three.searchintech.in
   ```
5. Apply §9, restart **only** QA, and re-run `bash deploy/qa/verify-qa.sh`.

Do not touch the existing `traffic-three` A record — it points at production and
re-pointing it *is* the Option B cutover.

---

## 11. How to ensure production remains untouched

**What isolates the two stacks**

| Global Docker resource | How QA avoids production |
|---|---|
| Compose project | `-p jnpa-uc3-poc-qa` vs `jnpa-uc3-poc` → separate networks and named volumes |
| Container names | `container_name: jnpa-qa-*` overrides the base file's `jnpa-*` |
| Host ports | every publish `!reset []`; only 18000 + 18080 are claimed |
| Image tags | built services retagged `jnpa/<name>:qa`, so a QA build never replaces `jnpa/<name>:0.1.0` |
| Database | `.env.qa` → `jnpa_qa`; `manage.sh` refuses to start on a `jnpa_schema_v3` DSN |
| Filesystem | QA runs from `/home/ec2-user/jnpa-uc3-poc-qa`; production's deploy rsyncs into `/home/ec2-user/jnpa-uc3-poc` |
| CI | `.github/workflows/deploy.yml` triggers on `main` only — pushing `qa` deploys nothing |

**Commands that must never be run from the QA checkout**

```
docker compose down                     # unscoped — resolves to the PRODUCTION project
docker compose -f ... down               # any compose command without -p jnpa-uc3-poc-qa
docker system prune / docker volume prune -a
docker stop $(docker ps -q)
docker rm -f jnpa-<anything-without-qa>

# and the one ordering mistake that matters:
docker compose -p jnpa-uc3-poc-qa -f docker-compose.yml \
  -f docker-compose.qa.yml -f docker-compose.aws.yml up      # QA overlay NOT last
                                                             # -> QA grabs 80/443
```
Also never run migrations, `DROP`/`TRUNCATE`/`DELETE`, or seed scripts against
`jnpa_schema_v3`, and never point `.env.qa` at it.

**Verification, before and after any QA operation**

```bash
# Production containers still up, with their original names and ports
docker ps --filter 'label=com.docker.compose.project=jnpa-uc3-poc' \
          --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'

# QA containers, separately
docker ps --filter 'label=com.docker.compose.project=jnpa-uc3-poc-qa' \
          --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'

# Production still answers
curl -fsS -o /dev/null -w 'prod web %{http_code}\n' https://traffic-three.searchintech.in/

# Production still on the production database
docker exec jnpa-gateway env | grep '^POSTGRES_DSN=' | sed -E 's/:[^:@/]*@/:****@/'   # .../jnpa_schema_v3

# Production image tags intact
docker images 'jnpa/*' --format '{{.Repository}}:{{.Tag}}' | sort

# Full sweep (includes the production checks above)
bash deploy/qa/verify-qa.sh
```

**Known interactions, both benign**

* The production deploy runs `up -d --remove-orphans`. `--remove-orphans` only
  removes containers carrying the *production* project label, so QA containers
  are never removed.
* The production deploy frees host ports 80 and 443 by removing whatever
  publishes them (`docker ps -q --filter publish=80`). QA publishes 18000/18080,
  so it is never matched.
* `deploy/aws/verify-rds.sh` filters containers by the substring `jnpa-`, which
  also matches `jnpa-qa-*`; while QA is running it will list QA containers as
  NOT-RDS. That is a reporting artefact of the prod script, not a fault — use
  `deploy/qa/verify-qa.sh` for QA.

---

## Files that make up this setup

| File | Purpose |
|---|---|
| `.env.qa.example` | QA environment template (RDS `jnpa_qa`, `APP_ENV=qa`, `<SET_ON_EC2_ONLY>` placeholders) |
| `docker-compose.qa.yml` | QA overlay: project `jnpa-uc3-poc-qa`, `jnpa-qa-*` names, `:qa` image tags, ports 18000/18080 |
| `web/nginx/default.qa.conf` | QA nginx — plain HTTP on 3000, proxies /api to the QA gateway |
| `deploy/qa/manage.sh` | up / down / restart / logs / ps / config / update, scoped to `jnpa-uc3-poc-qa` |
| `deploy/qa/verify-qa.sh` | proves QA → `jnpa_qa`, QA ports, and production intact |
| `tests/test_qa_deployment_contract.py` | static checks: QA never references `jnpa_schema_v3`, no port/name collisions, no secrets committed |
| `.gitignore` | ignores the filled-in `.env.qa`, keeps `.env.qa.example` tracked |

Unchanged: `docker-compose.yml`, `docker-compose.aws.yml`, `.env.aws.example`,
`web/nginx/default.conf`, `deploy/aws/*`, `.github/workflows/*`.
