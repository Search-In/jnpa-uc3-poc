# QA Deployment — JNPA UC-III PoC

How to stand up the **QA** stack of this repository on a **dedicated EC2
instance**, served over HTTPS at **https://qa.searchintech.in/**, against a
**separate database** (`jnpa_qa`) on the **same RDS instance** production uses.

Branch: `qa` · Compose project: `jnpa-uc3-poc-qa` · Database: `jnpa_qa` · Checkout: `~/jnpa-uc3-poc-qa`

| | Production | QA |
|---|---|---|
| EC2 instance | the production box | **its own instance** |
| Compose project | `jnpa-uc3-poc` | `jnpa-uc3-poc-qa` |
| Compose files | `docker-compose.yml` + `docker-compose.aws.yml` | the same two, **+ `docker-compose.qa.yml` last** |
| Env file | `.env` (from `.env.aws.example`) | `.env.qa` (from `.env.qa.example`) |
| Database | `jnpa_schema_v3` | `jnpa_qa` — **same RDS instance** |
| Container names | `jnpa-*` | `jnpa-qa-*` |
| Host ports | 80, 443, 3000, 8000, 8210 | **80, 443** (web) + **18000→8000** (gateway) |
| Built image tags | `jnpa/<name>:0.1.0` | `jnpa/<name>:qa` |
| URL | https://traffic-three.searchintech.in/ | **https://qa.searchintech.in/** |
| TLS | Let's Encrypt, in the web container | Let's Encrypt, in the web container |

Because QA is alone on its instance, its `web` container binds the host's 80/443
directly and the URL carries **no port suffix**. The one thing the two stacks
still share is the **RDS instance** — §9 covers what that means and what enforces
the separation.

> **Moving QA back onto a shared box?** Only two lines change: `web.ports` in
> `docker-compose.qa.yml` becomes `18080:80` / `18443:443`, and the redirect in
> `web/nginx/default.qa.conf` must then read `https://$host:18443$request_uri`
> — `$host` carries no port. `tests/test_qa_deployment_contract.py` enforces that
> pairing, so the test suite fails if you change one without the other.

---

## 1. Prerequisites

Before touching the instance:

| | What | Notes |
|---|---|---|
| **EC2** | Amazon Linux 2023, **≥16 GB RAM, ≥100 GB gp3** | The stack is ~30 containers — Kafka + Zookeeper, MinIO, Prometheus, Grafana, Jaeger and ~15 Python services. Check `free -m` / `df -h /` before the first `up`; the frontend build alone wants 8 GB. |
| **Security group (inbound)** | TCP **80** and **443** from `0.0.0.0/0` | 80 must be world-open — Let's Encrypt HTTP-01 validates over it. Leave **18000 closed**; use an SSH tunnel for gateway debugging. |
| **RDS** | this instance's SG allowed on **5432** | Add the QA instance's security group to the RDS inbound rules. The RDS instance must not be publicly accessible — `docs/RDS_SECURITY.md` §2. |
| **Route 53** | `qa.searchintech.in` **A** → the QA instance's public IP | Must resolve **before** certificate issuance. |
| **Database** | `jnpa_qa` exists, `jnpa_app` granted on it | The schema is already present; this deployment creates/migrates/drops nothing. |
| **Credentials to hand** | the RDS endpoint, the `jnpa_app` password | Neither is committed — `docs/RDS_SECURITY.md`, enforced by `tests/test_rds_security.py`. |

An **Elastic IP** is worth attaching. Without one, a stop/start changes the
public IP, the A record goes stale, and certificate renewal fails silently at
the next 60-day mark.

---

## 2. DNS

```bash
# Hosted zone searchintech.in -> Create record
#   Name  qa          Type A      Value <QA instance public IP>     TTL 300

dig +short qa.searchintech.in       # must print the QA instance's IP
```

Do **not** touch the `traffic-three` A record — that is production.

---

## 3. Bootstrap the instance

```bash
# git is not on the AL2023 base AMI
sudo dnf install -y git

cd /home/ec2-user
git clone -b qa https://github.com/Search-In/jnpa-uc3-poc.git jnpa-uc3-poc-qa
cd jnpa-uc3-poc-qa

RDS_HOST='<the RDS endpoint>' \
RDS_PASSWORD='<the jnpa_app password>' \
CERTBOT_EMAIL='ops@searchintech.in' \
  bash deploy/qa/bootstrap-ec2.sh
```

`deploy/qa/bootstrap-ec2.sh` is idempotent — re-run it after any partial
failure. It:

1. installs Docker + the compose plugin, Node 20 + pnpm (via corepack, so the
   version matches the `packageManager` pin in `package.json`), and certbot;
2. creates `/var/www/certbot`, the ACME webroot the QA nginx serves challenges from;
3. writes `.env.qa` — RDS/`jnpa_qa` DSNs via `deploy/aws/use-rds.sh`, generated
   secrets, `__QA_HOST__` substituted — then **fails loudly** if any placeholder
   survives or any DSN points at `jnpa_schema_v3`;
4. issues the Let's Encrypt certificate for `qa.searchintech.in`, after checking
   the name actually resolves to *this* instance;
5. installs a renewal deploy-hook that reloads the QA web container's nginx, plus
   a twice-daily systemd timer.

It deliberately does **not** start anything — the first `up` builds ~20 images
and you want to read the preflight output first.

**Log out and back in** afterwards so the `docker` group membership applies;
until you do, `docker` needs `sudo`.

<details>
<summary>Doing it by hand instead</summary>

```bash
cp .env.qa.example .env.qa && chmod 600 .env.qa

RDS_HOST='<endpoint>' RDS_DB=jnpa_qa RDS_USER=jnpa_app RDS_PASSWORD='<password>' \
  bash deploy/aws/use-rds.sh .env.qa
rm -f .env.qa.bak                       # same secrets, no reason to keep it

PAIR="$(openssl rand -hex 24)"
sed -i "s|MINIO_ACCESS_KEY=<SET_ON_EC2_ONLY>|MINIO_ACCESS_KEY=$(openssl rand -hex 16)|"           .env.qa
sed -i "s|MINIO_SECRET_KEY=<SET_ON_EC2_ONLY>|MINIO_SECRET_KEY=$(openssl rand -hex 24)|"           .env.qa
sed -i "s|GRAFANA_ADMIN_PASSWORD=<SET_ON_EC2_ONLY>|GRAFANA_ADMIN_PASSWORD=$(openssl rand -hex 16)|" .env.qa
sed -i "s|AUTH_JWT_SECRET=<SET_ON_EC2_ONLY>|AUTH_JWT_SECRET=$(openssl rand -hex 32)|"             .env.qa
sed -i "s|INTERNAL_SERVICE_TOKEN=<SET_ON_EC2_ONLY>|INTERNAL_SERVICE_TOKEN=$(openssl rand -hex 24)|" .env.qa
sed -i "s|PWA_PAIRING_SECRET=<SET_ON_EC2_ONLY>|PWA_PAIRING_SECRET=${PAIR}|"                       .env.qa
sed -i "s|VITE_PWA_PAIRING_SECRET=<SET_ON_EC2_ONLY>|VITE_PWA_PAIRING_SECRET=${PAIR}|"             .env.qa
sed -i "s|__QA_HOST__|qa.searchintech.in|g"                                                       .env.qa

# Nothing may remain unset
grep -n 'SET_ON_EC2_ONLY\|__QA_HOST__\|__RDS_HOST__\|<EC2_PUBLIC_IP>' .env.qa \
  && echo "^^ still unfilled" || echo "all placeholders replaced"

sudo mkdir -p /var/www/certbot/.well-known/acme-challenge
sudo certbot certonly --standalone -d qa.searchintech.in --agree-tos -m ops@searchintech.in
```

`PWA_PAIRING_SECRET` and `VITE_PWA_PAIRING_SECRET` **must be the same value** —
the PWA signs pairing payloads with the baked-in copy and the gateway verifies
with the runtime one.

</details>

`.env.qa` is git-ignored, so it can never be committed. The template
`.env.qa.example` stays tracked.

---

## 4. QA-specific environment values

Everything else in `.env.qa.example` matches `.env.aws.example`, so QA exercises
the production code paths. What differs:

```dotenv
APP_ENV=qa

# Postgres — same RDS instance, different database
POSTGRES_DSN=postgresql+asyncpg://jnpa_app:…@__RDS_HOST__:5432/jnpa_qa?ssl=require
RFID_POSTGRES_DSN=postgresql://jnpa_app:…@__RDS_HOST__:5432/jnpa_qa?sslmode=require
TRUCK_POSTGRES_DSN=…        CONGESTION_POSTGRES_DSN=…        ANOMALY_POSTGRES_DSN=…
GRAFANA_PG_HOST=__RDS_HOST__:5432
GRAFANA_PG_DB=jnpa_qa

# Public origin — bare hostname, no port: QA owns 443 on this box
MINIO_PUBLIC_ENDPOINT=qa.searchintech.in/minio
MINIO_PUBLIC_SECURE=true
ANOMALY_EVIDENCE_URL_BASE=https://qa.searchintech.in/minio/evidence
CORS_ALLOW_ORIGINS=https://qa.searchintech.in
```

Two DSN dialects are required and **must not be normalised to one**:
`POSTGRES_DSN` is a SQLAlchemy async engine over **asyncpg**, which accepts
`?ssl=require`; the `RFID_/TRUCK_/CONGESTION_/ANOMALY_` DSNs are **libpq/psycopg**
and need `?sslmode=require`.

`MINIO_SECURE` stays **false** — that is the *in-cluster* hop to `minio:9000`,
which is plain HTTP. Setting it true makes the client speak TLS to a plaintext
port and the gateway's startup gate dies with `[SSL: WRONG_VERSION_NUMBER]`. TLS
terminates at nginx, which is why `MINIO_PUBLIC_SECURE` is true and this one is not.

**`APP_ENV=qa` is production-like.** `gateway/auth.py` classifies anything outside
`development|dev|local|test` as production, so QA must also run
`AUTH_ENABLED=true`, a non-default `AUTH_JWT_SECRET` and `AUTH_DEV_TOKENS=false`
or the gateway refuses to start. The same classification makes `identity` require
the real ONNX models (§5).

---

## 5. Build the frontends and fetch the models

Both are prerequisites for the first `up`.

```bash
cd /home/ec2-user/jnpa-uc3-poc-qa

# 1. Frontend bundles. web/Dockerfile COPIES web/dist and mobile-pwa/dist — it
#    does not compile them — and refuses to build a non-live dashboard bundle.
pnpm install --frozen-lockfile=false
NODE_OPTIONS="--max-old-space-size=8192 --max-semi-space-size=512" \
  VITE_DATA_MODE=live VITE_AUTH_ENABLED=true \
  VITE_PWA_PAIRING_SECRET="$(grep '^PWA_PAIRING_SECRET=' .env.qa | cut -d= -f2-)" \
  pnpm -r --workspace-concurrency=1 build
bash scripts/verify_web_live_build.sh web/dist

# 2. Identity models — APP_ENV=qa is production-like, so `identity` crash-loops
#    without ArcFace + anti-spoof ONNX.
bash scripts/fetch_face_model.sh          # writes ./data/models
```

Build **one workspace at a time** (`--workspace-concurrency=1`) — parallel Vite
processes OOM the instance. If the box is memory-tight, build on a laptop or in
CI and `rsync` `web/dist` + `mobile-pwa/dist` up instead.

---

## 6. Start QA

**Validate the merged config first — never start on an unvalidated render.**

```bash
cd /home/ec2-user/jnpa-uc3-poc-qa
./deploy/qa/manage.sh config > /tmp/qa-config.yml && echo "config OK"

# Must print exactly 80, 443 and 18000
grep 'published:' /tmp/qa-config.yml

# Must print nothing (every container renamed)
grep 'container_name:' /tmp/qa-config.yml | grep -v 'jnpa-qa-'

# Must print .../jnpa_qa and nothing else
grep -oE '@[A-Za-z0-9._-]+:5432/[a-z_0-9]+' /tmp/qa-config.yml | sort -u

# Must print APP_ENV: qa
grep 'APP_ENV:' /tmp/qa-config.yml | sort -u

# Nothing QA wants may already be bound
sudo ss -ltnp | grep -E ':(80|443|18000)\b' || echo "80/443/18000 free"
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

* `-p jnpa-uc3-poc-qa` — separate compose project ⇒ separate network
  (`jnpa-uc3-poc-qa_jnpa`) and separate named volumes.
* `-f docker-compose.qa.yml` **last** — the QA overlay's `!override` entries only
  replace values from files applied **earlier**. Reorder the flags and QA would
  inherit the base file's `3000:3000` publish and the baked production nginx
  config. `manage.sh` always gets the order right.
* `--env-file .env.qa` — RDS `jnpa_qa`. `manage.sh` refuses to start if that file
  contains a `jnpa_schema_v3` DSN.

The first `up` builds ~20 images and takes a while; later runs are incremental.

### Why QA runs its own Redis / Kafka / MQTT / MinIO

They are **isolated, not shared**. Production's copies hold live state and, under
`docker-compose.aws.yml`, publish no host ports at all — they are reachable only
from inside production's own bridge network, on a different instance entirely.
QA therefore starts its own on the `jnpa-uc3-poc-qa_jnpa` network with no host
ports and `jnpa-qa-*` names. The cost is RAM.

---

## 7. Verify

One command checks the env file, the rendered config, the ports, the running
containers, a live database query, the certificate and every public route:

```bash
RDS_HOST='<the RDS endpoint>' bash deploy/qa/verify-qa.sh
# expects: RESULT: QA -> jnpa_qa, TLS on qa.searchintech.in, production intact ✔
```

Manual equivalents:

```bash
# --- TLS + public routes (from anywhere) ---
curl -I  https://qa.searchintech.in/                                     # 200, valid chain
curl -sI http://qa.searchintech.in/ | head -2                            # 301 -> https
curl -fsS -o /dev/null -w '%{http_code}\n' https://qa.searchintech.in/pwa/        # 200 driver PWA
curl -fsS -o /dev/null -w '%{http_code}\n' https://qa.searchintech.in/api/healthz # 200 via nginx -> gateway
openssl s_client -connect qa.searchintech.in:443 -servername qa.searchintech.in </dev/null 2>/dev/null \
  | openssl x509 -noout -subject -enddate

# --- gateway (on the box; 18000 is not open to the internet) ---
curl -fsS http://127.0.0.1:18000/healthz
curl -fsS http://127.0.0.1:18000/openapi.json | head -c 200
docker inspect -f '{{.State.Health.Status}}' jnpa-qa-gateway             # healthy
docker inspect -f '{{.State.Health.Status}}' jnpa-qa-web                 # healthy

# --- database ---
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
asyncio.run(m())"                                                        # db=jnpa_qa ssl=True
```

Authenticated routes returning **401 without a bearer token is correct** — QA
runs `AUTH_ENABLED=true`.

To reach the gateway on 18000 without opening it to the internet:

```bash
ssh -L 18000:localhost:18000 ec2-user@qa.searchintech.in
```

Do **not** use `deploy/aws/verify-rds.sh` for QA: its container filter is the
substring `jnpa-`, which also matches `jnpa-qa-*`, so it reports QA containers as
NOT-RDS. `deploy/qa/verify-qa.sh` filters by the exact compose project label.

---

## 8. Day-2 operations

```bash
cd /home/ec2-user/jnpa-uc3-poc-qa

./deploy/qa/manage.sh ps          # QA status
./deploy/qa/manage.sh logs        # follow QA gateway + web
./deploy/qa/manage.sh restart     # restart QA services
./deploy/qa/manage.sh down        # stop + remove QA containers (volumes kept)
./deploy/qa/manage.sh update      # git pull + rebuild changed images + up

docker restart jnpa-qa-gateway    # single service, by container name
docker exec jnpa-qa-web nginx -s reload    # after editing default.qa.conf
```

`web/nginx/default.qa.conf` is bind-mounted, so an nginx change needs only a
reload — not a rebuild. **`manage.sh update` runs `git pull`**, which will
overwrite local edits to that file; commit them on the `qa` branch instead.

### Certificate renewal

Handled by the systemd timer `bootstrap-ec2.sh` installs — twice daily, via the
webroot, with a deploy hook that reloads the container's nginx. Renewal needs no
downtime because the `:80` server block serves `/.well-known/acme-challenge/`
from the same `/var/www/certbot` certbot writes to.

```bash
systemctl list-timers certbot-renew.timer
sudo certbot renew --dry-run            # exercises the whole path, issues nothing
sudo certbot certificates               # expiry dates
```

If renewal ever fails, the usual cause is DNS: the A record no longer points at
this instance (a stop/start without an Elastic IP).

---

## 9. Keeping production safe

QA is on its own instance, so container names, host ports, image tags and the
filesystem can no longer collide. **The RDS instance is still shared**, and that
is the one thing left to get wrong.

| Shared resource | What separates QA from production |
|---|---|
| **RDS instance** | `.env.qa` → `jnpa_qa`; `deploy/qa/manage.sh` **refuses to start** if any DSN in it matches `jnpa_schema_v3`; `tests/test_qa_deployment_contract.py` fails the build if `.env.qa.example` ever names the production database |
| Database role | `jnpa_app`, the least-privilege application role — no DDL, no cross-database reads (`docs/RDS_SECURITY.md` §3) |
| Repository | `.github/workflows/deploy.yml` triggers on `main` only — pushing `qa` deploys nothing to production |
| Compose project | `-p jnpa-uc3-poc-qa` → its own network and named volumes |
| Container names / image tags | `jnpa-qa-*`, `jnpa/<name>:qa` — kept so the stack stays co-locatable without a redesign |

Never run migrations, `DROP`/`TRUNCATE`/`DELETE`, or seed scripts against
`jnpa_schema_v3`, and never point `.env.qa` at it.

```bash
# The DSN check, on demand
grep -E '^(POSTGRES_DSN|RFID_|TRUCK_|CONGESTION_|ANOMALY_)' .env.qa | sed -E 's/:[^:@/]*@/:****@/'

# Production still answers, from the QA box
curl -fsS -o /dev/null -w 'prod web %{http_code}\n' https://traffic-three.searchintech.in/
```

Now that the stacks are on different instances, `docker system prune` and
`docker compose down` on the QA box can no longer reach production — but
`manage.sh` still has no `nuke`, and everything it runs stays scoped to the
`jnpa-uc3-poc-qa` project.

---

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `jnpa-qa-web` restarts in a loop, logs `cannot load certificate` | the certificate was not issued before the first `up` | `sudo certbot certonly --standalone -d qa.searchintech.in` with the stack down, then `manage.sh up` |
| Browser shows the site but driver photos are broken; console says *mixed content* | `MINIO_PUBLIC_SECURE=false` or a `http://` / port-suffixed `MINIO_PUBLIC_ENDPOINT` | set `MINIO_PUBLIC_ENDPOINT=qa.searchintech.in/minio` + `MINIO_PUBLIC_SECURE=true`, restart the gateway |
| Photos 403 with `SignatureDoesNotMatch` | `MINIO_PUBLIC_ENDPOINT` is not the host the browser sends — SigV4 signs the host | make it match the browser's origin exactly, no port |
| Gateway dies at boot with `[SSL: WRONG_VERSION_NUMBER]` | `MINIO_SECURE=true` — the in-cluster hop to `minio:9000` is plain HTTP | set `MINIO_SECURE=false` (§4) |
| `jnpa-qa-web` reports `unhealthy` while serving fine | the probe followed a redirect into a hostname mismatch | the healthcheck must target `/healthz` on `:80`, which is exempt from the redirect |
| `certbot` fails with an authorization error | `qa.searchintech.in` does not resolve here, or SG blocks 80 | `dig +short qa.searchintech.in`; open 80 to `0.0.0.0/0` |
| `port is already allocated` on `up` | something else holds 80/443 | `sudo ss -ltnp \| grep -E ':(80\|443)\b'` |
| `identity` crash-loops | ONNX models missing — `APP_ENV=qa` is production-like | `bash scripts/fetch_face_model.sh` |
| `web` build refuses with a non-live bundle error | `VITE_DATA_MODE` was not `live` | rebuild per §5, then `bash scripts/verify_web_live_build.sh web/dist` |
| Gateway cannot reach RDS | the QA instance's SG is not on the RDS inbound rules | add it; `docs/RDS_SECURITY.md` §2 |

---

## Files that make up this setup

| File | Purpose |
|---|---|
| `.env.qa.example` | QA environment template — RDS `jnpa_qa`, `APP_ENV=qa`, `__QA_HOST__` / `__RDS_HOST__` / `<SET_ON_EC2_ONLY>` placeholders |
| `docker-compose.qa.yml` | QA overlay: project `jnpa-uc3-poc-qa`, `jnpa-qa-*` names, `:qa` image tags, web on 80/443, gateway on 18000 |
| `web/nginx/default.qa.conf` | QA nginx — TLS for `qa.searchintech.in`, ACME webroot, `/healthz`, proxies `/api` to the QA gateway |
| `deploy/qa/bootstrap-ec2.sh` | one-shot, idempotent instance prep: docker, certbot, `.env.qa`, certificate, renewal hook |
| `deploy/qa/manage.sh` | up / down / restart / logs / ps / config / update, scoped to `jnpa-uc3-poc-qa` |
| `deploy/qa/verify-qa.sh` | proves QA → `jnpa_qa`, the expected ports, TLS, and production intact |
| `deploy/aws/use-rds.sh` | writes the six Postgres DSN lines (shared with production; `RDS_DB=jnpa_qa` for QA) |
| `tests/test_qa_deployment_contract.py` | static checks: QA never references `jnpa_schema_v3`, no name/tag collisions, nginx TLS is QA's own, no secrets committed |
| `.gitignore` | ignores the filled-in `.env.qa`, keeps `.env.qa.example` tracked |

Unchanged: `docker-compose.yml`, `docker-compose.aws.yml`, `.env.aws.example`,
`web/nginx/default.conf`, `deploy/aws/*` (other than reuse), `.github/workflows/*`.
