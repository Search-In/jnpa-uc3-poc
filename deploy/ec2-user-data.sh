#!/usr/bin/env bash
# ============================================================================
# JNPA UC-III PoC — EC2 user-data bootstrap (Amazon Linux 2023).
#
# Paste this into the "User data" field when launching the instance (Advanced
# details), OR run it manually as root after first SSH. It is IDEMPOTENT — safe
# to re-run.
#
# What it does:
#   1. Installs Docker Engine + the compose plugin + git.
#   2. Finds the SECOND EBS volume (the dedicated data disk you attached) and
#      mounts it at /var/lib/docker, so ALL images + named volumes (postgres,
#      minio, kafka, trained models…) live on durable, snapshot-able EBS rather
#      than the root disk. Formats it ONLY if it is blank (never wipes data).
#   3. Clones the repo to /opt/jnpa-uc3-poc.
#
# It deliberately does NOT start the stack — you still need to drop in real
# secrets (.env.prod) first. The last lines print exactly what to do next.
#
# Before pasting, set the two vars in the CONFIG block below.
# ============================================================================
set -euxo pipefail

# ---------------------------- CONFIG ----------------------------------------
REPO_URL="https://github.com/Aniket29-shiv/jnpa-uc3-poc.git"
REPO_REF="aws"                 # branch or tag to deploy
DATA_DEVICE_HINT="/dev/nvme1n1" # the attached data EBS (Nitro instances name it nvmeXn1)
APP_DIR="/opt/jnpa-uc3-poc"
# ----------------------------------------------------------------------------

# Detect the package manager (AL2023 = dnf). This script targets Amazon Linux.
if command -v dnf >/dev/null 2>&1; then PKG=dnf; else PKG=yum; fi

# 1) Docker + compose plugin + git ------------------------------------------
$PKG -y update || true
$PKG -y install docker git
# The compose v2 plugin location for Amazon Linux.
mkdir -p /usr/libexec/docker/cli-plugins
COMPOSE_VER="v2.29.7"
curl -fsSL "https://github.com/docker/compose/releases/download/${COMPOSE_VER}/docker-compose-linux-$(uname -m)" \
  -o /usr/libexec/docker/cli-plugins/docker-compose
chmod +x /usr/libexec/docker/cli-plugins/docker-compose

systemctl enable --now docker
# Let ec2-user run docker without sudo (effective on next login).
usermod -aG docker ec2-user || true

# 2) Mount the dedicated data EBS at /var/lib/docker -------------------------
# Resolve the data device. On Nitro, the root is /dev/nvme0n1 and the first
# extra volume is /dev/nvme1n1. Fall back to scanning for an unmounted disk.
DATA_DEV=""
if [ -b "$DATA_DEVICE_HINT" ]; then
  DATA_DEV="$DATA_DEVICE_HINT"
else
  # Pick the first whole disk that is NOT the root disk and has no mountpoint.
  ROOT_DISK="$(lsblk -no PKNAME "$(findmnt -no SOURCE /)" 2>/dev/null || true)"
  for d in $(lsblk -dno NAME,TYPE | awk '$2=="disk"{print $1}'); do
    [ "$d" = "$ROOT_DISK" ] && continue
    if [ -z "$(lsblk -no MOUNTPOINT "/dev/$d" | tr -d ' ')" ]; then
      DATA_DEV="/dev/$d"; break
    fi
  done
fi

if [ -n "$DATA_DEV" ] && [ -b "$DATA_DEV" ]; then
  # Format ONLY if the device has no filesystem yet (protects existing data).
  if ! blkid "$DATA_DEV" >/dev/null 2>&1; then
    mkfs -t xfs "$DATA_DEV"
  fi
  systemctl stop docker || true
  mkdir -p /var/lib/docker
  # Persist by UUID so it survives device-name reshuffles across reboots.
  UUID="$(blkid -s UUID -o value "$DATA_DEV")"
  if ! grep -q "$UUID" /etc/fstab; then
    echo "UUID=$UUID  /var/lib/docker  xfs  defaults,nofail  0  2" >> /etc/fstab
  fi
  mount -a
  systemctl start docker
else
  echo "WARN: no separate data EBS found; Docker will use the root volume." >&2
fi

# 3) Clone the repo ----------------------------------------------------------
if [ ! -d "$APP_DIR/.git" ]; then
  git clone "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"
git fetch --all --tags
git checkout "$REPO_REF"
git pull --ff-only || true
chown -R ec2-user:ec2-user "$APP_DIR"

# 4) Next-step banner --------------------------------------------------------
cat <<'EOF' > /etc/motd

  ===========================================================================
  JNPA UC-III PoC — bootstrap complete.  Next steps (as ec2-user):

    cd /opt/jnpa-uc3-poc
    cp deploy/.env.prod.example .env.prod
    nano .env.prod                 # set the __CHANGE_ME__ secrets + PUBLIC_BASE_URL
    deploy/jnpa-uc3.sh up          # build + start (first build ~10-20 min)
    deploy/jnpa-uc3.sh health      # wait for HTTP 200

  Dashboard (behind the ALB/Caddy) -> https://<your-domain>/live
  Admin UIs are localhost-only; reach them via an SSH tunnel, e.g.:
    ssh -L 3001:localhost:3001 -L 16686:localhost:16686 ec2-user@<ip>
  ===========================================================================
EOF
echo "BOOTSTRAP_DONE"
