# Deploying JNPA UC3 on AWS — single EC2 (cheapest demo)

This is the **manual, AWS-side runbook** for Use Case III. All the application config (web image + nginx, the AWS compose overlay, the bootstrap script, and management utilities) already lives in the repo — this doc covers only what you do in the AWS console / CLI.

**What you get:** one EC2 instance running the whole stack via `docker compose`, with nginx serving the dashboard on port 80 and the driver PWA on `/pwa`, while reverse-proxying `/api` requests to the gateway container.

```
Internet ──▶ EC2 :80 (nginx in `web` container)
                ├─ serves the Control-Room Dashboard (/)
                ├─ serves the Driver PWA (/pwa)
                └─ proxies /api/* ─▶ gateway:8000
            docker compose network (internal, not exposed):
                gateway · 10+ backend capability services · AI models (congestion + anomaly)
                kafka + zookeeper · postgres(TimescaleDB) · redis · mosquitto · minio
```

---

## What's already in the repo (no action needed)

| File | Purpose |
|---|---|
| `web/Dockerfile` | builds the dashboard and PWA, serves them via nginx |
| `web/nginx/default.conf` | static serving + `/api` and `/api/ws` reverse-proxy |
| `docker-compose.aws.yml` | overlay: maps `web` to port 80, hides internal ports, sets `restart: unless-stopped` for all 28 services |
| `.env.aws.example` | template for the EC2 `.env` |
| `deploy/aws/user-data.sh` | EC2 bootstrap (installs Docker, fetches code, auto-generates secure passwords, brings the stack up) |
| `deploy/aws/manage.sh` | `up` / `down` / `logs` / `update` wrapper |

---

## Step 0 — Prerequisites (one time)

- An AWS account + a region (e.g. `ap-south-1` Mumbai — closest to JNPA).
- An **EC2 key pair** for SSH (`AWS Console → EC2 → Key Pairs → Create`). Download the `.pem`.
- A way to get the code onto the box. Pick one:
  - **A — Git:** Push this repo somewhere the box can clone (e.g., a private GitHub repo). 
    Since this is a private repo, you must generate a dedicated SSH deploy key pair:
    1. **Generate SSH Key locally:**
       ```bash
       ssh-keygen -t ed25519 -C "deploy-key-uc3" -f ./id_ed25519_uc3
       ```
    2. **Register public key on GitHub:** Go to your repository settings in GitHub → **Deploy keys** → **Add deploy key**. Paste the contents of `id_ed25519_uc3.pub` (leave "Allow write access" unchecked).
    3. **Store private key in SSM Parameter Store:**
       Create a parameter named `/jnpa/uc3_deploy_key` of type `SecureString` containing the contents of the private key (`id_ed25519_uc3`).
    4. **Configure IAM permissions:** Ensure your EC2 Instance Profile has permissions to retrieve the key. Add the following policy statement to the EC2's IAM role:
       ```json
       {
           "Version": "2012-10-17",
           "Statement": [
               {
                   "Effect": "Allow",
                   "Action": [
                       "ssm:GetParameter"
                   ],
                   "Resource": "arn:aws:ssm:*:*:parameter/jnpa/uc3_deploy_key"
               }
           ]
       }
       ```
  - **B — Copy:** `scp`/`rsync` the working tree up after the box is running (works with zero git setup — see Step 4B).

---

## Step 1 — Pick the instance size

The Use Case 3 stack is **28 containers** (gateway, 10+ capability services, 3 AI services including PyTorch/PaddleOCR, Kafka, Postgres/TimescaleDB, Redis, Mosquitto, MinIO, and nginx). It is RAM-bound.

| Instance | vCPU / RAM | Cost (ap-south-1, on-demand) | Verdict |
|---|---|---|---|
| `t3.medium` | 2 / 4 GB | ~$30/mo | ⚠️ **minimum** — stack will run but might OOM during heavy GNN/autoencoder training |
| **`t3.large`** | 2 / 8 GB | **~$60/mo** | ✅ **recommended** for stable execution of the 28-container stack |
| `t3.xlarge` | 4 / 16 GB | ~$120/mo | comfortable headroom for concurrent What-If scenario simulations |

**EBS:** The Docker images, build stages, database tables, and AI model weights require significant storage. Bump the root volume to at least **30 GB gp3** at launch.

You can stop the instance between demos to pay only for storage (~$2.40/mo for 30 GB).

---

## Step 2 — Security Group

Create a security group (`jnpa-uc3-demo`) with **inbound**:

| Type | Port | Source | Why |
|---|---|---|---|
| HTTP | 80 | `0.0.0.0/0` (or your office IP) | the dashboard & driver PWA |
| SSH | 22 | **your IP only** | admin |
| HTTPS | 443 | `0.0.0.0/0` | only if you add TLS (Step 7) |

Leave outbound as default (all). Nothing else needs to be open — Postgres, Kafka, Redis, Mosquitto, MinIO, and the backend services stay completely safe on the internal Docker network.

---

## Step 3 — Launch the instance

`EC2 → Launch instance`:

1. **Name:** `jnpa-uc3-demo`
2. **AMI:** Amazon Linux 2023 (x86_64). (The bootstrap also handles `arm64`/Graviton — a `t4g.large` is ~20% cheaper if you prefer ARM.)
3. **Type:** `t3.large` (per Step 1)
4. **Key pair:** the one from Step 0
5. **Network:** attach the `jnpa-uc3-demo` security group
6. **Storage:** root volume → **30 GB gp3**
7. **Advanced → User data:** paste the contents of `deploy/aws/user-data.sh`.
   - If using **Git (option A)**, edit the top of the script first: set
     `REPO_URL="https://github.com/<you>/<repo>.git"` (and `REPO_BRANCH` if not `main`).
   - If using **copy (option B)**, leave `REPO_URL` empty and skip ahead — you'll
     run the bootstrap by hand in Step 4B.

Launch.

---

## Step 4 — Get the stack running

### 4A — Git path (you set REPO_URL in user-data)

User-data runs automatically on first boot. It installs Docker, clones the repo to `/opt/jnpa-uc3`, writes a starter `.env` (automatically generating secure, random credentials for Postgres and MinIO), and runs `up -d --build`. The first build pulls base images and compiles the web bundle — give it **4–8 minutes**.

Check progress:

```bash
ssh -i your-key.pem ec2-user@<EC2_PUBLIC_IP>
sudo cat /var/log/cloud-init-output.log   # bootstrap log
cd /opt/jnpa-uc3 && ./deploy/aws/manage.sh ps
```

### 4B — Copy path (no git on the box)

```bash
# from your laptop, in the repo root:
rsync -az --exclude node_modules --exclude .venv --exclude '.git' \
  -e "ssh -i your-key.pem" ./ ec2-user@<EC2_PUBLIC_IP>:/opt/jnpa-uc3/

ssh -i your-key.pem ec2-user@<EC2_PUBLIC_IP>
cd /opt/jnpa-uc3
sudo bash deploy/aws/user-data.sh      # installs Docker, writes .env, brings stack up
```

---

## Step 5 — Configure secrets (do this before sharing the URL)

The bootstrap copies `.env.aws.example → .env` and generates secure database/storage passwords. **Review and customize** keys for live endpoints:

```bash
cd /opt/jnpa-uc3
sudo nano .env          # set MAPBOX_TOKEN, external API keys, etc.
./deploy/aws/manage.sh up   # re-applies .env (rebuilds only what changed)
```

Key environment variables to configure:

| Variable | Recommended Action |
|---|---|
| `MAPBOX_TOKEN` | Provide a style token to render the Mapbox basemap (otherwise falls back to Bhuvan WMS). |
| `GOOGLE_MAPS_API_KEY` | Set to enable live traffic flow predictions (falls back to synthetic baseline when empty). |
| `OPENWEATHER_API_KEY` | Set to retrieve live meteorological data (rain/dust/fog) for the ANPR feed. |
| `SUREPASS_API_TOKEN` | Set to hook up real Vahan KYC queries (falls back to `vahan-sim` when empty). |
| `ULIP_API_KEY` | Set to connect real ULIP trucking telemetry (falls back to GPS simulators when empty). |

---

## Step 6 — Verify

```bash
# on the box
./deploy/aws/manage.sh ps          # all 28 services Up / healthy
curl -s localhost/api/vahan/rc/MH04AB1234  # gateway answers through nginx reverse proxy
```

Then open the following URLs in a browser:
- **Dashboard:** `http://<EC2_PUBLIC_IP>/`
- **Driver PWA:** `http://<EC2_PUBLIC_IP>/pwa` (add `?device=DEV-000001` to test a virtual vehicle driver)

You should see the live congestion panels and active vehicle tracks.

---

## Step 7 — (Optional) HTTPS + a real hostname

The `web` container listens on plain `:80`. For a shareable HTTPS link, easiest paths:

- **Caddy in front** (auto Let's Encrypt): point a DNS A-record at the EIP, run a Caddy container reverse-proxying `:443 → web:3000`.
- **CloudFront** in front of the instance (origin = the public DNS, HTTP) — gives HTTPS + a `*.cloudfront.net` name with no DNS of your own.
- **ALB** with an ACM cert if you want a managed load balancer (adds ~$16/mo).

**Elastic IP:** allocate one and associate it so the URL survives a stop/start (`EC2 → Elastic IPs → Allocate → Associate`). ~free while attached to a running instance; small charge if allocated but unused.

---

## Day-2 operations

Run these commands from `/opt/jnpa-uc3` on the EC2 box:

```bash
./deploy/aws/manage.sh logs       # follow gateway + web logs
./deploy/aws/manage.sh update     # git pull + rebuild changed images (git path)
./deploy/aws/manage.sh restart    # bounce all services
./deploy/aws/manage.sh down       # stop containers, keep DB/minio volumes
./deploy/aws/manage.sh nuke       # down + delete volumes (wipes DB/MinIO)
```

**Pause between demos (save money):** `EC2 → Stop instance`. With an Elastic IP the URL is preserved; `restart: unless-stopped` brings all 28 containers back online automatically on `Start`. You pay only for EBS (~$2.40/mo) while stopped.

**Tear down completely:** `manage.sh nuke` → `EC2 → Terminate instance` → release the Elastic IP → delete the security group.

---

## Cost summary (ap-south-1, on-demand)

| Item | Running 24×7 | Demo-only (stopped between) |
|---|---|---|
| t3.large | ~$60/mo | ~$0.10/hr while on |
| 30 GB gp3 EBS | ~$2.40/mo | ~$2.40/mo (charged while stopped too) |
| Elastic IP | free (attached) | small charge if instance stopped |
| **Typical** | **~$62.40/mo** | **~$4–10/mo** if used a few hours/week |

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| First build very slow / containers killed | Out-of-memory pressure on a small box. Ensure you are using at least `t3.large`. |
| Dashboard loads but panels are empty | Gateway or Postgres not reachable. Check status using `manage.sh ps` and logs using `manage.sh logs`. |
| `502 Bad Gateway` on `/api/...` | Gateway container is still initializing (cold start, waiting for Kafka/Postgres to become healthy). Wait 30 seconds and retry. |
| `no space left on device` | EBS volume filled up with build cache. Grow the root volume to 30 GB (`EC2 → Volumes → Modify`) then run `sudo growpart /dev/nvme0n1 1` and `sudo xfs_growfs /` (or `resize2fs` on ext4). |
