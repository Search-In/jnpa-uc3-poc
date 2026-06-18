#!/usr/bin/env bash
# ============================================================================
# JNPA UC3 — EC2 user-data bootstrap (Amazon Linux 2023).
# Paste this into the "User data" field when launching the instance.
# It configures swap space and installs Docker, Compose, and Buildx so the 
# instance is ready to receive deployments from GitHub Actions.
# ============================================================================
set -euo pipefail

log() { echo "[bootstrap] $*"; }

# --- 0. Configure Swap Space (Prevents OOM during build/execution) ----------
if [ ! -f /swapfile ]; then
  log "creating 4GB swap file"
  fallocate -l 4G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=4096
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile swap swap defaults 0 0' >> /etc/fstab
  log "swap space configured successfully"
else
  log "swap file already exists"
fi

# --- 1. Docker engine + Git -------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  log "installing docker and git"
  dnf update -y
  dnf install -y docker git
  systemctl enable --now docker
  usermod -aG docker ec2-user || true
fi

# --- 2. Docker Compose plugin -----------------------------------------------
if ! docker compose version >/dev/null 2>&1; then
  log "installing docker compose plugin"
  mkdir -p /usr/local/lib/docker/cli-plugins
  ARCH="$(uname -m)"; case "$ARCH" in aarch64) CARCH=aarch64;; *) CARCH=x86_64;; esac
  curl -fsSL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-${CARCH}" \
    -o /usr/local/lib/docker/cli-plugins/docker-compose
  chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
fi

# --- 3. Docker Buildx plugin ------------------------------------------------
log "installing/updating docker buildx plugin"
mkdir -p /usr/local/lib/docker/cli-plugins
ARCH="$(uname -m)"; case "$ARCH" in aarch64) CARCH=arm64;; *) CARCH=amd64;; esac
curl -fsSL "https://github.com/docker/buildx/releases/download/v0.19.2/buildx-v0.19.2.linux-${CARCH}" \
  -o /usr/local/lib/docker/cli-plugins/docker-buildx
chmod +x /usr/local/lib/docker/cli-plugins/docker-buildx

log "EC2 bootstrap complete! The instance is ready for GitHub Actions deployments."
