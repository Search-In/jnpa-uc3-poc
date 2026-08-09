#!/usr/bin/env bash
# =============================================================================
# bootstrap-ec2.sh — prepare a FRESH, DEDICATED QA EC2 instance to run the JNPA
# UC-III QA stack at https://qa.searchintech.in/.
#
#   # on the new instance, as ec2-user, from the QA checkout:
#   RDS_HOST=<rds endpoint> RDS_PASSWORD='<jnpa_app password>' \
#   CERTBOT_EMAIL=ops@searchintech.in \
#     bash deploy/qa/bootstrap-ec2.sh
#
# It does NOT start the stack. It gets the box to the point where
# `./deploy/qa/manage.sh up` is the only remaining step, then tells you so —
# the first `up` builds ~20 images and you want to read the preflight output
# before committing to that.
#
# WHAT IT DOES, all idempotent (safe to re-run after a partial failure):
#   1. installs docker + compose plugin, Node 20 + pnpm (corepack), and certbot
#   2. creates the ACME webroot the QA nginx serves challenges from
#   3. writes .env.qa from .env.qa.example: RDS/jnpa_qa DSNs, generated secrets,
#      __QA_HOST__ substituted. An EXISTING .env.qa is left alone (it holds
#      generated secrets that must not churn) — delete it to force a rewrite.
#   4. issues the Let's Encrypt certificate for $QA_HOST
#   5. installs a deploy hook so `certbot renew` reloads the QA web container
#
# WHAT IT DELIBERATELY DOES NOT DO:
#   * start, build or stop any container
#   * create DNS records (do that first — step 4 needs the A record to resolve)
#   * open security-group ports (80/443 inbound must already be allowed)
#   * touch any production host, database or certificate
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/../.."
REPO_ROOT="$PWD"

QA_HOST="${QA_HOST:-qa.searchintech.in}"
QA_DB="${QA_DB:-jnpa_qa}"
RDS_USER="${RDS_USER:-jnpa_app}"
ENV_FILE="${ENV_FILE:-.env.qa}"
WEBROOT="${WEBROOT:-/var/www/certbot}"
QA_WEB_CONTAINER="${QA_WEB_CONTAINER:-jnpa-qa-web}"
SKIP_DOCKER="${SKIP_DOCKER:-0}"
SKIP_CERT="${SKIP_CERT:-0}"

for arg in "$@"; do
  case "$arg" in
    --skip-docker) SKIP_DOCKER=1 ;;
    --skip-cert)   SKIP_CERT=1 ;;
    -h|--help)     sed -n '2,32p' "$0"; exit 0 ;;
    *)             echo "unknown argument: $arg" >&2; exit 1 ;;
  esac
done

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '   OK  %s\n' "$*"; }
warn() { printf '   !!  %s\n' "$*" >&2; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------- #
say "0. Preflight"
# --------------------------------------------------------------------------- #
[[ -f docker-compose.qa.yml ]] || die "run this from the QA checkout (docker-compose.qa.yml not found)"
sudo -n true 2>/dev/null || sudo true || die "this script needs sudo"

# Refuse to run on the production box. Bootstrapping here would install a QA
# certificate renewal hook next to production's and fight for port 80.
if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx 'jnpa-web'; then
  die "a PRODUCTION jnpa-web container is running on this box. This script is for a
  dedicated QA instance only — QA would take port 80/443 from it."
fi

branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
[[ "$branch" == "qa" ]] || warn "checkout is on branch '${branch}', not 'qa'"
ok "repo: ${REPO_ROOT} (branch ${branch})"
ok "QA hostname: ${QA_HOST}"

# --------------------------------------------------------------------------- #
say "1. Docker + Node/pnpm + certbot"
# --------------------------------------------------------------------------- #
pkg=""
command -v dnf >/dev/null 2>&1 && pkg=dnf
command -v yum >/dev/null 2>&1 && [[ -z "$pkg" ]] && pkg=yum
[[ -n "$pkg" ]] || die "no dnf/yum — install docker, the compose plugin and certbot by hand"

if [[ "$SKIP_DOCKER" == "1" ]]; then
  ok "skipped (--skip-docker)"
elif command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  ok "docker + compose plugin already installed"
else
  sudo "$pkg" install -y docker
  # The compose *plugin* (`docker compose`, not `docker-compose`). Amazon Linux
  # 2023 ships it as docker-compose-plugin; older AMIs need the manual drop-in.
  if ! docker compose version >/dev/null 2>&1; then
    sudo "$pkg" install -y docker-compose-plugin 2>/dev/null || {
      warn "no docker-compose-plugin package — installing the plugin binary directly"
      cli_dir=/usr/libexec/docker/cli-plugins
      sudo mkdir -p "$cli_dir"
      sudo curl -fsSL \
        "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$(uname -m)" \
        -o "$cli_dir/docker-compose"
      sudo chmod +x "$cli_dir/docker-compose"
    }
  fi
  sudo systemctl enable --now docker
  # So `docker` works without sudo. Needs a new login shell to take effect —
  # every docker call in THIS run still goes through sudo for that reason.
  sudo usermod -aG docker "$USER" || true
  ok "docker installed and enabled (log out/in for group membership)"
fi
sudo docker compose version >/dev/null 2>&1 || die "docker compose plugin still unavailable"

# buildx is a SEPARATE plugin from compose, and Amazon Linux 2023's `docker`
# package ships neither. `docker compose build` hard-fails with "compose build
# requires buildx 0.17.0 or later" without it — after pulling every base image,
# so the failure looks like a build error rather than a missing plugin.
if sudo docker buildx version >/dev/null 2>&1; then
  ok "buildx already installed ($(sudo docker buildx version | head -1))"
elif [[ "$SKIP_DOCKER" == "1" ]]; then
  ok "buildx: skipped (--skip-docker)"
else
  sudo "$pkg" install -y docker-buildx-plugin 2>/dev/null || {
    warn "no docker-buildx-plugin package — installing the plugin binary directly"
    cli_dir=/usr/libexec/docker/cli-plugins
    sudo mkdir -p "$cli_dir"
    case "$(uname -m)" in
      x86_64)  bx_arch=amd64 ;;
      aarch64) bx_arch=arm64 ;;
      *)       die "unsupported architecture $(uname -m) for the buildx binary" ;;
    esac
    # The release asset embeds its own version, so resolve the tag first rather
    # than guessing at a /latest/download/ filename.
    bx_ver="$(curl -fsSL https://api.github.com/repos/docker/buildx/releases/latest \
              | grep -m1 '"tag_name"' | sed -E 's/.*"([^"]+)".*/\1/')"
    [[ -n "$bx_ver" ]] || die "could not resolve the latest buildx release tag"
    sudo curl -fsSL \
      "https://github.com/docker/buildx/releases/download/${bx_ver}/buildx-${bx_ver}.linux-${bx_arch}" \
      -o "$cli_dir/docker-buildx"
    sudo chmod +x "$cli_dir/docker-buildx"
  }
  sudo docker buildx version >/dev/null 2>&1 || die "buildx still unavailable"
  ok "buildx installed ($(sudo docker buildx version | head -1))"
fi

# Node + pnpm. Needed because web/Dockerfile COPIES web/dist and
# mobile-pwa/dist rather than compiling them — the bundles must be built on the
# box (or rsynced in) before the first `up`. pnpm comes from corepack so the
# version matches the `packageManager` pin in package.json exactly; a mismatched
# pnpm re-resolves the lockfile and can pull different transitive versions than
# CI tested.
if command -v pnpm >/dev/null 2>&1; then
  ok "node $(node --version 2>/dev/null) + pnpm $(pnpm --version 2>/dev/null) already installed"
else
  if ! command -v node >/dev/null 2>&1; then
    sudo "$pkg" install -y nodejs20 2>/dev/null || sudo "$pkg" install -y nodejs
  fi
  command -v node >/dev/null 2>&1 || die "node install failed — install Node 20 by hand"
  node_major="$(node --version | sed -E 's/^v([0-9]+).*/\1/')"
  [[ "$node_major" -ge 18 ]] || warn "node $(node --version) is older than 18; the Vite build may fail"
  sudo corepack enable 2>/dev/null || sudo npm install -g corepack@latest
  # Reads the pinned version out of package.json and installs exactly that.
  sudo corepack prepare --activate 2>/dev/null || true
  command -v pnpm >/dev/null 2>&1 || die "pnpm unavailable after corepack enable"
  ok "node $(node --version) + pnpm $(pnpm --version)"
fi

if [[ "$SKIP_CERT" == "1" ]]; then
  ok "certbot: skipped (--skip-cert)"
elif command -v certbot >/dev/null 2>&1; then
  ok "certbot already installed"
elif sudo "$pkg" install -y certbot 2>/dev/null && command -v certbot >/dev/null 2>&1; then
  ok "certbot installed from $pkg"
else
  # Upstream's documented fallback for distros without a certbot package.
  warn "no certbot package — installing into a venv at /opt/certbot"
  sudo "$pkg" install -y python3 python3-pip
  sudo python3 -m venv /opt/certbot
  sudo /opt/certbot/bin/pip install --upgrade pip certbot
  sudo ln -sf /opt/certbot/bin/certbot /usr/bin/certbot
  ok "certbot installed into /opt/certbot"
fi

# --------------------------------------------------------------------------- #
say "2. ACME webroot"
# --------------------------------------------------------------------------- #
# docker-compose.qa.yml bind-mounts this read-only into the web container, and
# the :80 server block serves /.well-known/acme-challenge/ from it. It must
# exist BEFORE the first `up` — Docker would otherwise create it root-owned
# with the wrong semantics, and `certbot renew --webroot` writes here as root.
sudo mkdir -p "${WEBROOT}/.well-known/acme-challenge"
sudo chmod -R 755 "$WEBROOT"
ok "${WEBROOT} ready"

# --------------------------------------------------------------------------- #
say "3. ${ENV_FILE}"
# --------------------------------------------------------------------------- #
if [[ -f "$ENV_FILE" ]]; then
  ok "${ENV_FILE} already exists — left untouched (delete it to regenerate)"
else
  [[ -n "${RDS_HOST:-}" ]] || die "RDS_HOST is required to write ${ENV_FILE}
  (the endpoint is deliberately not committed — docs/RDS_SECURITY.md)"
  [[ -n "${RDS_PASSWORD:-}" ]] || die "RDS_PASSWORD is required to write ${ENV_FILE}
  (URL-encode any of  : / ? # [ ] @  in it)"

  cp .env.qa.example "$ENV_FILE"
  chmod 600 "$ENV_FILE"

  # Reuse the existing writer for the six Postgres lines — it is the same code
  # path production uses, and it leaves every other line alone.
  RDS_HOST="$RDS_HOST" RDS_DB="$QA_DB" RDS_USER="$RDS_USER" RDS_PASSWORD="$RDS_PASSWORD" \
    bash deploy/aws/use-rds.sh "$ENV_FILE" >/dev/null
  rm -f "${ENV_FILE}.bak"        # holds the same secrets, and we just wrote them

  # Generated secrets. PWA_PAIRING_SECRET and its VITE_ twin MUST match — the
  # PWA signs pairing payloads with the baked-in copy and the gateway verifies
  # with the runtime one.
  pair="$(openssl rand -hex 24)"
  set_secret() { sed -i "s|^${1}=<SET_ON_EC2_ONLY>$|${1}=${2}|" "$ENV_FILE"; }
  set_secret MINIO_ACCESS_KEY         "$(openssl rand -hex 16)"
  set_secret MINIO_SECRET_KEY         "$(openssl rand -hex 24)"
  set_secret GRAFANA_ADMIN_PASSWORD   "$(openssl rand -hex 16)"
  set_secret AUTH_JWT_SECRET          "$(openssl rand -hex 32)"
  set_secret INTERNAL_SERVICE_TOKEN   "$(openssl rand -hex 24)"
  set_secret PWA_PAIRING_SECRET       "$pair"
  set_secret VITE_PWA_PAIRING_SECRET  "$pair"

  # The public origin: MINIO_PUBLIC_ENDPOINT, ANOMALY_EVIDENCE_URL_BASE,
  # CORS_ALLOW_ORIGINS. Bare hostname, no port — QA owns 443 on this box.
  sed -i "s|__QA_HOST__|${QA_HOST}|g" "$ENV_FILE"

  ok "${ENV_FILE} written (mode 600)"
fi

# Whatever path we took, nothing may remain unsubstituted. Match only ASSIGNMENT
# lines — the template's own comments legitimately name the placeholders they
# tell you to replace ("# Replace <EC2_PUBLIC_IP> with …"), and matching those
# fails a correctly-filled env file.
placeholder_re='^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*=.*(SET_ON_EC2_ONLY|__QA_HOST__|__RDS_HOST__|<EC2_PUBLIC_IP>)'
if leftover="$(grep -nE "$placeholder_re" "$ENV_FILE")"; then
  printf '%s\n' "$leftover" >&2
  die "^^ ${ENV_FILE} still has unfilled placeholders"
fi
ok "no unfilled placeholders"

# The one mistake that would let QA write to production data.
grep -qE '^[A-Z_]*POSTGRES_DSN=.*jnpa_schema_v3' "$ENV_FILE" \
  && die "${ENV_FILE} points at the PRODUCTION database"
grep -qE "^POSTGRES_DSN=.*/${QA_DB}\?" "$ENV_FILE" \
  || die "${ENV_FILE} POSTGRES_DSN does not target ${QA_DB}"
ok "DSNs target ${QA_DB}"

# --------------------------------------------------------------------------- #
say "4. TLS certificate for ${QA_HOST}"
# --------------------------------------------------------------------------- #
if [[ "$SKIP_CERT" == "1" ]]; then
  ok "skipped (--skip-cert)"
elif sudo test -d "/etc/letsencrypt/live/${QA_HOST}"; then
  ok "certificate already present"
  sudo openssl x509 -in "/etc/letsencrypt/live/${QA_HOST}/fullchain.pem" \
       -noout -subject -enddate 2>/dev/null | sed 's/^/       /'
else
  [[ -n "${CERTBOT_EMAIL:-}" ]] || die "CERTBOT_EMAIL is required to issue a certificate
  (Let's Encrypt uses it for expiry warnings)"

  # HTTP-01 needs the name to resolve to THIS box before we ask for the cert;
  # otherwise the challenge is served by whatever the A record points at and
  # certbot fails after burning a rate-limit slot.
  resolved="$(getent hosts "$QA_HOST" | awk '{print $1; exit}' || true)"
  public_ip="$(curl -fsS --max-time 5 \
    -H "X-aws-ec2-metadata-token: $(curl -fsS --max-time 5 -X PUT \
      -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' \
      http://169.254.169.254/latest/api/token 2>/dev/null)" \
    http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || true)"
  if [[ -z "$resolved" ]]; then
    die "${QA_HOST} does not resolve. Create the Route 53 A record first (QA_DEPLOYMENT.md §3)."
  elif [[ -n "$public_ip" && "$resolved" != "$public_ip" ]]; then
    die "${QA_HOST} resolves to ${resolved}, but this instance is ${public_ip}.
  Point the A record here and wait for the TTL before re-running."
  fi
  ok "${QA_HOST} -> ${resolved} (this instance)"

  # Standalone binds :80 itself and is right for the FIRST issue, before the
  # stack exists. Once the web container is up it owns :80, so go through the
  # webroot it is already serving instead of taking the site down.
  if sudo docker ps --format '{{.Names}}' | grep -qx "$QA_WEB_CONTAINER"; then
    ok "${QA_WEB_CONTAINER} is running — using --webroot"
    sudo certbot certonly --webroot -w "$WEBROOT" -d "$QA_HOST" \
      --non-interactive --agree-tos -m "$CERTBOT_EMAIL"
  else
    sudo ss -ltn 2>/dev/null | grep -qE ':80\s' \
      && die "something is already listening on :80; stop it or use --webroot by hand"
    ok "nothing on :80 — using --standalone"
    sudo certbot certonly --standalone -d "$QA_HOST" \
      --non-interactive --agree-tos -m "$CERTBOT_EMAIL"
  fi
  ok "certificate issued"
fi

# --------------------------------------------------------------------------- #
say "5. Renewal hook"
# --------------------------------------------------------------------------- #
# nginx caches the certificate in memory at startup, so a renewed file on disk
# changes nothing until it reloads. `reload` is graceful — no dropped requests.
# Guarded on the container being up: during a `manage.sh down` the hook must
# not fail the renewal.
hook=/etc/letsencrypt/renewal-hooks/deploy/reload-jnpa-qa-web.sh
sudo mkdir -p "$(dirname "$hook")"
sudo tee "$hook" >/dev/null <<EOF
#!/usr/bin/env bash
# Installed by deploy/qa/bootstrap-ec2.sh. Reloads the QA web container's nginx
# after a successful certificate renewal so it picks up the new file.
set -eu
if docker ps --format '{{.Names}}' | grep -qx "${QA_WEB_CONTAINER}"; then
  docker exec "${QA_WEB_CONTAINER}" nginx -s reload
fi
EOF
sudo chmod +x "$hook"
ok "$hook"

# certbot's package ships a systemd timer; the venv install does not. Create one
# if it is missing, so renewal is not left to a human either way.
if systemctl list-unit-files 2>/dev/null | grep -qE '^(certbot-renew|certbot)\.timer'; then
  sudo systemctl enable --now certbot-renew.timer 2>/dev/null \
    || sudo systemctl enable --now certbot.timer 2>/dev/null || true
  ok "renewal timer enabled (packaged)"
else
  sudo tee /etc/systemd/system/certbot-renew.service >/dev/null <<EOF
[Unit]
Description=Renew Let's Encrypt certificates (JNPA QA)
[Service]
Type=oneshot
ExecStart=/usr/bin/certbot renew --webroot -w ${WEBROOT} --quiet
EOF
  sudo tee /etc/systemd/system/certbot-renew.timer >/dev/null <<'EOF'
[Unit]
Description=Twice-daily Let's Encrypt renewal check
[Timer]
# Twice daily is what Let's Encrypt asks for; the randomised delay keeps every
# instance in the fleet from hitting the ACME API at the same second.
OnCalendar=*-*-* 00,12:00:00
RandomizedDelaySec=3600
Persistent=true
[Install]
WantedBy=timers.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable --now certbot-renew.timer
  ok "renewal timer installed and enabled"
fi

# --------------------------------------------------------------------------- #
say "Done — the box is ready. Remaining steps:"
# --------------------------------------------------------------------------- #
cat <<EOF

  1. Build the frontend bundles (web/Dockerfile copies them, it does not compile
     them, and it REFUSES to build a non-live dashboard bundle):

       pnpm install --frozen-lockfile=false
       NODE_OPTIONS="--max-old-space-size=8192 --max-semi-space-size=512" \\
         VITE_DATA_MODE=live VITE_AUTH_ENABLED=true \\
         VITE_PWA_PAIRING_SECRET="\$(grep '^PWA_PAIRING_SECRET=' ${ENV_FILE} | cut -d= -f2-)" \\
         pnpm -r --workspace-concurrency=1 build
       bash scripts/verify_web_live_build.sh web/dist

  2. Fetch the identity ONNX models (APP_ENV=qa is production-like, so the
     identity service crash-loops without them):

       bash scripts/fetch_face_model.sh

  3. Validate the merged config, then start:

       ./deploy/qa/manage.sh config | grep -E 'published:|container_name:'
       ./deploy/qa/manage.sh up

  4. Verify:

       RDS_HOST=<rds endpoint> bash deploy/qa/verify-qa.sh
       curl -I https://${QA_HOST}/

  Security group: 80 and 443 inbound must be open to the world (Let's Encrypt
  validates over 80). Keep 18000 closed and use an SSH tunnel for gateway debug.
  RDS: this instance's security group must be allowed on 5432.

EOF
