# `deploy/` — AWS single-EC2 deployment

Everything needed to run the UC-III PoC on one AWS EC2 instance with the
existing `docker compose` stack, behind HTTPS.

| File | What it is |
|---|---|
| **[`AWS_DEPLOY_GUIDE.md`](AWS_DEPLOY_GUIDE.md)** | **Start here.** The step-by-step AWS-console guide (Security Groups, EC2, EBS, ACM, ALB, DNS, costs, troubleshooting). |
| [`ec2-user-data.sh`](ec2-user-data.sh) | EC2 launch bootstrap — installs Docker, mounts the data EBS at `/var/lib/docker`, clones the repo. Paste into the instance's *User data* field. |
| [`.env.prod.example`](.env.prod.example) | Production env template. Copy to `.env.prod` on the box and fill the `__CHANGE_ME__` secrets + `PUBLIC_BASE_URL`. |
| [`docker-compose.prod.yml`](docker-compose.prod.yml) | Compose override for prod (Grafana password from env, evidence URLs → public origin). Overlaid on the base `docker-compose.yml`. |
| [`jnpa-uc3.sh`](jnpa-uc3.sh) | Lifecycle helper on the box: `up`, `down`, `update`, `health`, `backup`, `logs`, `nuke`. Wraps the two-file compose invocation. |
| [`caddy/Caddyfile`](caddy/Caddyfile) | Optional: TLS on the box via Caddy + Let's Encrypt, instead of an ALB (cheaper). See guide Appendix B. |

## TL;DR

1. Read [`AWS_DEPLOY_GUIDE.md`](AWS_DEPLOY_GUIDE.md) and do the AWS-console steps
   (Security Groups → EC2 with the user-data script → Elastic IP → ACM → ALB → DNS).
2. On the box:
   ```bash
   cd /opt/jnpa-uc3-poc
   cp deploy/.env.prod.example .env.prod && nano .env.prod   # secrets + PUBLIC_BASE_URL
   deploy/jnpa-uc3.sh up
   deploy/jnpa-uc3.sh health
   ```
3. Open `https://<your-domain>/live`.

The exposure boundary is the **EC2 Security Group**: only the web port (via the
ALB) is public; admin UIs (Grafana, Jaeger, Kafka-UI, MinIO) are SSH-tunnel only.
