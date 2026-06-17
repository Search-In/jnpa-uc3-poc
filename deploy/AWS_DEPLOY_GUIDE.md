# JNPA UC-III PoC — AWS Deployment Guide

This is the **manual, AWS-console side** of deploying the UC-III PoC. The
repo-side config (a production compose override, an EC2 bootstrap script, a
lifecycle helper, and an optional Caddy front-end) is already committed under
[`deploy/`](.). Follow this guide top-to-bottom; each step says whether it's a
one-time click in the AWS console or a command on the box.

---

## 0. Architecture (what you're deploying)

```
        Internet
           │  HTTPS (443)
           ▼
   ┌──────────────────┐      ACM cert (uc3.example.com)
   │  Application LB   │◄─────────────────────────────────
   │   (ALB, public)   │
   └────────┬─────────┘
            │  HTTP :3000  (only the web container is exposed)
            ▼
   ┌─────────────────────────────────────────────────────────┐
   │  EC2 instance  (Amazon Linux 2023, ~8 vCPU / 32 GB)      │
   │                                                          │
   │   docker compose  — the full UC-III stack (~22 services) │
   │   ┌──────────┐  web:3000  → nginx → /api → gateway:8000  │
   │   │ web      │                                            │
   │   ├──────────┤  gateway, scenarios, anpr, congestion,    │
   │   │ services │  anomaly, truck-sim, vahan-*, rfid-*, …    │
   │   ├──────────┤  postgres/timescale, kafka, redis, minio,  │
   │   │ infra    │  mosquitto, prometheus, grafana, jaeger     │
   │   └──────────┘                                            │
   │                                                          │
   │   Root EBS (OS) + Data EBS (mounted at /var/lib/docker)  │
   │                  ↑ all volumes + images live here,        │
   │                    snapshot this for backups              │
   └─────────────────────────────────────────────────────────┘
```

Key decisions (already made): **single EC2 box running the existing compose
stack**, **all stateful infra stays as containers** (Postgres/Timescale, Kafka,
Redis, MinIO) on a dedicated EBS data volume, and **HTTPS via an ALB + an ACM
certificate** on your domain. Only the web container faces the load balancer;
every other port is reachable only on-box or over an SSH tunnel.

> **Don't want the ALB cost?** There's a drop-in alternative: run **Caddy** on
> the box for automatic Let's Encrypt TLS — see [Appendix B](#appendix-b--tls-without-an-alb-caddy).
> It saves ~\$16–22/mo but you manage the cert host yourself.

---

## 1. Prerequisites

- An AWS account with permission to create EC2, EBS, VPC/Security Groups, an
  ALB, an ACM certificate, and (optionally) a Route 53 record.
- A **domain name you control** (for HTTPS). It can live in Route 53 or any
  registrar — you just need to point a record at the ALB.
- An **EC2 key pair** (for SSH). Create one under *EC2 → Key Pairs* if you don't
  have one; download the `.pem`.
- The repo pushed to a Git host the instance can `git clone` (it's already at
  `https://github.com/Aniket29-shiv/jnpa-uc3-poc.git`, branch `aws`). If you
  make it private, see [Appendix C](#appendix-c--private-repo-access).

---

## 2. Pick the instance size

This stack runs **three CPU-only AI services** (ANPR/YOLO, congestion GNN+LSTM,
anomaly autoencoder — all pull `torch`) plus a **truck simulator that reserves
3 vCPUs** for 20k–30k devices, plus Kafka + Postgres/Timescale + MinIO. It is
**RAM- and CPU-hungry for a PoC**.

| Instance | vCPU | RAM | Notes | Est. on-demand (ap-south-1)* |
|---|---|---|---|---|
| `t3.2xlarge` | 8 | 32 GB | **Recommended.** Comfortable; burst credits cover the AI first-boot training spike. | ~\$0.33/hr ≈ **\$240/mo** |
| `m6i.2xlarge` | 8 | 32 GB | Steady (non-burst) CPU — pick this if you'll run it 24/7. | ~\$0.40/hr ≈ **\$290/mo** |
| `m6i.4xlarge` | 16 | 64 GB | Headroom for the 30k-fleet stress + smooth demos. | ~\$0.80/hr ≈ **\$580/mo** |
| `t3.xlarge` | 4 | 16 GB | **Minimum.** Works only if you cut `TRUCK_NUM_DEVICES` to ~5k and accept slow first-boot AI training. Risky for a live demo. | ~\$0.17/hr ≈ **\$120/mo** |

\* Prices are rough ap-south-1 (Mumbai) on-demand figures for orientation only —
check the current AWS pricing page. **You only pay while it runs**: stop the
instance between demos and you pay just for EBS storage (a few \$/mo).

**Recommendation: `t3.2xlarge` (8 vCPU / 32 GB).** Start there; bump to
`m6i.4xlarge` only if a demo feels sluggish under the full 30k fleet.

Storage: **30 GB root** (gp3) + a **50 GB data volume** (gp3) for
`/var/lib/docker` (images + all named volumes). The torch images alone are
several GB.

---

## 3. Create the Security Group (the network boundary)

This is the **authoritative firewall** — the compose stack publishes many ports
on the host, but only what this SG allows inbound is reachable from outside the
box. Create **two** security groups under *EC2 → Security Groups*:

### SG-A: `jnpa-uc3-alb` (for the load balancer)
| Type | Protocol | Port | Source | Why |
|---|---|---|---|---|
| HTTPS | TCP | 443 | `0.0.0.0/0` | Public demo traffic |
| HTTP | TCP | 80 | `0.0.0.0/0` | Redirect → HTTPS |

### SG-B: `jnpa-uc3-ec2` (for the instance)
| Type | Protocol | Port | Source | Why |
|---|---|---|---|---|
| Custom TCP | TCP | 3000 | **SG-A** (the ALB's SG, not an IP) | Only the ALB may reach the web container |
| SSH | TCP | 22 | **your.ip.addr/32** | Admin + SSH tunnels to the admin UIs. Lock to your IP. |

> **Do NOT** open 8080/3001/9000/9092/16686/8000/etc. to the internet. Those are
> Kafka-UI, Grafana, MinIO, the broker, Jaeger, the gateway, and the raw service
> APIs. Reach them over an SSH tunnel instead (see [§9](#9-reach-the-admin-uis-grafana-jaeger-kafka-ui-minio)).
> Leaving Kafka or MinIO open to the world is a real exposure.

If you skip the ALB and use Caddy instead, SG-B also needs **80 and 443 from
`0.0.0.0/0`** (Caddy needs 80 for the ACME challenge). See Appendix B.

---

## 4. Launch the EC2 instance

*EC2 → Instances → Launch instances*:

1. **Name:** `jnpa-uc3-poc`
2. **AMI:** *Amazon Linux 2023* (x86_64). The bootstrap script targets AL2023's
   `dnf`. (If you prefer Ubuntu, swap the package commands in
   [`deploy/ec2-user-data.sh`](ec2-user-data.sh) — `apt-get` + the Docker apt repo.)
3. **Instance type:** `t3.2xlarge` (from §2).
4. **Key pair:** select your key pair.
5. **Network settings → Firewall:** *Select existing security group* → **SG-B
   (`jnpa-uc3-ec2`)**.
6. **Storage:**
   - Root volume: **30 GB gp3**.
   - *Add new volume*: **50 GB gp3** — this is the data disk the bootstrap
     mounts at `/var/lib/docker`. (Note its device; on Nitro it shows up inside
     the OS as `/dev/nvme1n1`.)
7. **Advanced details → User data:** paste the **entire contents** of
   [`deploy/ec2-user-data.sh`](ec2-user-data.sh). Before pasting, open it and
   confirm the CONFIG block at the top:
   - `REPO_URL` — your repo (default is correct).
   - `REPO_REF` — `aws` (or `main` once merged).
   - `DATA_DEVICE_HINT` — `/dev/nvme1n1` for a Nitro instance with one extra
     volume (correct for the setup above).
8. **Launch.** Allocate an **Elastic IP** (*EC2 → Elastic IPs → Allocate →
   Associate* to this instance) so the public IP survives stop/start.

The user-data script runs once at first boot: installs Docker + the compose
plugin + git, formats & mounts the data EBS at `/var/lib/docker`, and clones the
repo to `/opt/jnpa-uc3-poc`. It takes ~2–3 min; watch it with
`sudo tail -f /var/log/cloud-init-output.log` after SSHing in.

---

## 5. Configure secrets and bring the stack up (on the box)

SSH in (`ssh -i your-key.pem ec2-user@<elastic-ip>`), then:

```bash
cd /opt/jnpa-uc3-poc

# 1. Create the production env from the template and fill in the secrets.
cp deploy/.env.prod.example .env.prod
nano .env.prod
#   - Replace every __CHANGE_ME__ (DB password, MinIO keys, Grafana password).
#     The DB password must match in BOTH POSTGRES_PASSWORD and POSTGRES_DSN.
#   - Set PUBLIC_BASE_URL=https://uc3.example.com  (your real domain).
#   - Leave all the external API keys blank for the offline demo (DATA_MODE=mock).

# 2. Build + start the whole stack (first build pulls torch + Chromium ~10-20 min).
deploy/jnpa-uc3.sh up

# 3. Wait until the dashboard answers (polls http://localhost:3000/).
deploy/jnpa-uc3.sh health
```

When `health` prints `OK (200)` the stack is serving locally. It is **not yet
public** — that's the ALB in §6–7.

Useful lifecycle commands (all via `deploy/jnpa-uc3.sh`):

```bash
deploy/jnpa-uc3.sh ps            # container status
deploy/jnpa-uc3.sh logs web      # tail one service
deploy/jnpa-uc3.sh update        # git pull + rebuild + rolling restart
deploy/jnpa-uc3.sh down          # stop, KEEP data
deploy/jnpa-uc3.sh backup        # pg_dump + minio tar into ./backups
deploy/jnpa-uc3.sh nuke          # stop + DELETE all data volumes (full reset)
```

---

## 6. Request the TLS certificate (ACM)

*Certificate Manager (ACM) → Request → Public certificate*. **Request it in the
same region as the ALB.**

1. Domain name: `uc3.example.com` (and optionally `*.example.com`).
2. Validation: **DNS validation** (recommended).
3. ACM gives you a CNAME record to add at your DNS provider. If the domain is in
   Route 53, click **"Create record in Route 53"** and it's automatic.
4. Wait for status **Issued** (usually a few minutes).

---

## 7. Create the Application Load Balancer

*EC2 → Load Balancers → Create → Application Load Balancer*:

1. **Name:** `jnpa-uc3-alb`. Scheme: **internet-facing**. IP type: IPv4.
2. **Network:** your VPC; select **≥2 public subnets** (ALBs need two AZs).
3. **Security group:** **SG-A (`jnpa-uc3-alb`)**.
4. **Listeners & routing → create a target group** first (open in a new tab):
   - Type: **Instances**. Protocol **HTTP**, port **3000**.
   - **Health check path: `/`** (the dashboard returns 200 at `/`). Healthy
     threshold 2, interval 15s is fine.
   - Register the `jnpa-uc3-poc` instance, port **3000**. Create.
5. Back in the ALB wizard:
   - **HTTPS:443 listener** → forward to the target group → select the **ACM
     certificate** from §6.
   - **HTTP:80 listener** → *Redirect to HTTPS:443* (permanent 301).
6. Create the ALB. Note its **DNS name** (`jnpa-uc3-alb-xxxx.<region>.elb.amazonaws.com`).

> **WebSocket note:** the dashboard's live updates use `/api/ws`. ALBs support
> WebSockets natively on the HTTP/HTTPS listener — no extra config needed. The
> nginx in the web image already upgrades the connection.

---

## 8. Point your domain at the ALB (DNS)

At your DNS provider, create a record for `uc3.example.com`:

- **Route 53:** an **A record, Alias = Yes**, target = the ALB. (Alias is free
  and resolves to the ALB's changing IPs.)
- **Other registrar:** a **CNAME** `uc3 → jnpa-uc3-alb-xxxx.<region>.elb.amazonaws.com`.

Propagation is usually a minute or two. Then open **`https://uc3.example.com/live`**
— the control-room dashboard. The PWA is at `https://uc3.example.com/pwa`.

✅ At this point the demo is live over HTTPS.

---

## 9. Reach the admin UIs (Grafana, Jaeger, Kafka-UI, MinIO)

These are intentionally **not public**. Tunnel to them over SSH:

```bash
ssh -i your-key.pem \
  -L 3001:localhost:3001 \   # Grafana
  -L 16686:localhost:16686 \ # Jaeger traces
  -L 8080:localhost:8080 \   # Kafka-UI
  -L 9101:localhost:9101 \   # MinIO console
  -L 8000:localhost:8000 \   # gateway API (debug)
  ec2-user@<elastic-ip>
```

Then in your browser: Grafana → `http://localhost:3001`, Jaeger →
`http://localhost:16686`, etc. (Grafana login is the `GRAFANA_ADMIN_*` you set
in `.env.prod`.)

---

## 10. Backups & durability

All durable state (Postgres/Timescale, MinIO objects, Kafka logs, trained model
weights, Grafana) lives in Docker named volumes, which sit on the **data EBS
volume** mounted at `/var/lib/docker`. So:

- **Authoritative backup = an EBS snapshot of the data volume.** Either on demand
  (*EC2 → Volumes → select the data volume → Create snapshot*) or scheduled via
  **Amazon Data Lifecycle Manager** (e.g. daily, retain 7).
- **Logical export (portable):** `deploy/jnpa-uc3.sh backup` writes a
  `pg_dump` + a MinIO tarball into `./backups/<timestamp>/`. Good for moving data
  off-box; the EBS snapshot is the real disaster-recovery path.

To **stop paying for compute between demos:** *EC2 → Stop instance*. The Elastic
IP, EBS volumes, and all data persist; you pay only for storage. Start it again
and run `deploy/jnpa-uc3.sh up` (data + trained models are still there, so it
comes up fast — no retraining).

---

## 11. Updating the deployment

After pushing new commits:

```bash
ssh -i your-key.pem ec2-user@<elastic-ip>
cd /opt/jnpa-uc3-poc
deploy/jnpa-uc3.sh update      # git pull + rebuild changed images + restart + health-check
```

---

## Cost summary (rough, ap-south-1, running 24/7)

| Item | Est. monthly |
|---|---|
| `t3.2xlarge` EC2 (24/7) | ~\$240 |
| EBS 30 GB root + 50 GB data (gp3) | ~\$8 |
| Application Load Balancer | ~\$16–22 |
| ACM certificate | **\$0** (free) |
| Data transfer (light demo use) | a few \$ |
| **Total (always-on)** | **~\$270–290/mo** |
| **Total (stopped between demos)** | **~\$10–25/mo** (EBS + ALB only) |

Cut it further: stop the instance when idle (biggest lever), drop the ALB for
Caddy (Appendix B) to save ~\$20/mo, or use a Savings Plan / Reserved Instance if
it'll run continuously.

---

## Appendix A — Quick checklist

**AWS console (one-time):**
- [ ] Key pair created/downloaded
- [ ] SG-A (`jnpa-uc3-alb`: 80/443 from world) created
- [ ] SG-B (`jnpa-uc3-ec2`: 3000 from SG-A, 22 from your IP) created
- [ ] EC2 `t3.2xlarge`, AL2023, 30 GB root + 50 GB data, SG-B, user-data pasted
- [ ] Elastic IP allocated + associated
- [ ] ACM cert for `uc3.example.com` → Issued
- [ ] Target group (HTTP:3000, health `/`) + instance registered
- [ ] ALB (internet-facing, SG-A) with HTTPS:443→TG and HTTP:80→redirect
- [ ] DNS A-alias / CNAME `uc3.example.com` → ALB
- [ ] (Optional) DLM snapshot schedule on the data volume

**On the box (one-time):**
- [ ] `cp deploy/.env.prod.example .env.prod` + fill secrets + `PUBLIC_BASE_URL`
- [ ] `deploy/jnpa-uc3.sh up` → `deploy/jnpa-uc3.sh health` → `OK (200)`
- [ ] `https://uc3.example.com/live` loads

---

## Appendix B — TLS without an ALB (Caddy)

If you'd rather not run an ALB, terminate TLS on the box with Caddy (automatic
Let's Encrypt). Trade-off: you manage the cert host, and the box is directly
internet-facing on 80/443.

1. In **SG-B**, add inbound **80** and **443** from `0.0.0.0/0` (80 is required
   for the ACME HTTP-01 challenge). Remove the "3000 from SG-A" rule — there's no
   ALB.
2. Point DNS **A record** `uc3.example.com` → the **Elastic IP** (not an ALB).
3. Edit [`deploy/caddy/Caddyfile`](caddy/Caddyfile): replace `uc3.example.com`
   and the email.
4. Run Caddy alongside the stack:
   ```bash
   cd /opt/jnpa-uc3-poc
   docker run -d --name jnpa-caddy --restart unless-stopped --network host \
     -v "$PWD/deploy/caddy/Caddyfile:/etc/caddy/Caddyfile:ro" \
     -v jnpa-caddy-data:/data -v jnpa-caddy-config:/config \
     caddy:2
   ```
   Caddy reaches the web container at `localhost:3000` (host networking) and
   serves HTTPS on 443. The cert provisions automatically on first request.

Skip ACM (§6), the ALB (§7), and use this DNS target instead of the ALB in §8.

---

## Appendix C — Private repo access

If you make the GitHub repo private, the instance can't `git clone` over HTTPS
without a credential. Easiest options:

- **Deploy key:** add an SSH deploy key to the repo, put the private key on the
  box at `/home/ec2-user/.ssh/id_ed25519`, and set `REPO_URL` to the SSH form
  (`git@github.com:Aniket29-shiv/jnpa-uc3-poc.git`) in `ec2-user-data.sh`.
- **Fine-grained PAT:** `REPO_URL=https://<token>@github.com/Aniket29-shiv/jnpa-uc3-poc.git`
  (token with read-only Contents scope). Keep the token out of user-data logs by
  cloning manually after first SSH instead of via user-data.

---

## Appendix D — Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| ALB target shows **unhealthy** | Stack still building (first build is slow) — wait, then `deploy/jnpa-uc3.sh ps`. Confirm the web container is up and health-check path is `/` on port 3000. |
| Dashboard loads but **no live updates** | WebSocket blocked. ALB handles WS automatically; if using Caddy, confirm the Caddyfile `@ws` block. Check `deploy/jnpa-uc3.sh logs gateway`. |
| **Out of memory** / containers restarting | Instance too small for the AI services + 20k fleet. Bump to `m6i.4xlarge`, or lower `TRUCK_NUM_DEVICES` in `.env.prod`. |
| First boot very slow | Expected: the AI services train on first start (no weights yet). Subsequent starts reuse the persisted weights and are fast. |
| `GRAFANA_ADMIN_PASSWORD` error on `up` | You skipped it in `.env.prod`. The prod override requires it (no default admin/admin in the cloud). |
| Can't reach Grafana/Kafka-UI in a browser | Correct — they're not public. Use the SSH tunnel in §9. |
| Evidence images 404 in the dashboard | `PUBLIC_BASE_URL` not set or wrong in `.env.prod`; it rewrites the anomaly evidence URLs. |
```
