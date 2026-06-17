#!/usr/bin/env bash
# ============================================================================
# JNPA UC-III PoC — production lifecycle helper (single-EC2 / docker compose).
#
# Wraps the two-file compose invocation so you never type the long form. Run it
# from the repo root on the EC2 box:
#
#   deploy/jnpa-uc3.sh up         # build + start the whole stack (detached)
#   deploy/jnpa-uc3.sh down       # stop, KEEP data volumes
#   deploy/jnpa-uc3.sh nuke       # stop + DELETE data volumes (full reset)
#   deploy/jnpa-uc3.sh ps         # container status
#   deploy/jnpa-uc3.sh logs [svc] # tail logs (optionally one service)
#   deploy/jnpa-uc3.sh update     # git pull + rebuild + rolling restart
#   deploy/jnpa-uc3.sh health     # wait until the public endpoint answers 200
#   deploy/jnpa-uc3.sh backup     # dump postgres + minio to ./backups
#   deploy/jnpa-uc3.sh exec <svc> <cmd...>   # exec into a service container
# ============================================================================
set -euo pipefail

# Resolve repo root (this script lives in <root>/deploy).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

ENV_FILE="${ENV_FILE:-.env.prod}"
BASE_COMPOSE="docker-compose.yml"
PROD_COMPOSE="deploy/docker-compose.prod.yml"
PUBLIC_PORT="${PUBLIC_PORT:-3000}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found. Copy deploy/.env.prod.example to .env.prod and fill the secrets." >&2
  exit 1
fi

# docker compose v2 ("docker compose") vs legacy ("docker-compose").
if docker compose version >/dev/null 2>&1; then
  DC=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  DC=(docker-compose)
else
  echo "ERROR: neither 'docker compose' nor 'docker-compose' is installed." >&2
  exit 1
fi

compose() {
  "${DC[@]}" --env-file "$ENV_FILE" -f "$BASE_COMPOSE" -f "$PROD_COMPOSE" "$@"
}

cmd="${1:-help}"; shift || true

case "$cmd" in
  up)
    echo ">> Building + starting the JNPA UC-III stack (this can take 10-20 min on first build: torch + Chromium)…"
    compose build
    compose up -d
    echo ">> Stack is starting. Watch health with:  deploy/jnpa-uc3.sh health"
    ;;

  down)
    echo ">> Stopping the stack (data volumes are KEPT)…"
    compose down
    ;;

  nuke)
    read -r -p "This DELETES all data volumes (postgres, minio, models, grafana). Type 'nuke' to confirm: " ans
    [[ "$ans" == "nuke" ]] || { echo "aborted."; exit 1; }
    compose down -v
    ;;

  ps)      compose ps "$@" ;;
  logs)    compose logs -f --tail=200 "$@" ;;
  exec)    svc="$1"; shift; compose exec "$svc" "$@" ;;
  config)  compose config "$@" ;;   # render the merged config (sanity check)

  update)
    echo ">> Pulling latest source…"
    git pull --ff-only
    echo ">> Rebuilding changed images…"
    compose build
    echo ">> Rolling restart…"
    compose up -d
    echo ">> Done. Verifying health…"
    exec "$0" health
    ;;

  health)
    echo -n ">> Waiting for the dashboard on http://localhost:${PUBLIC_PORT}/ "
    for i in $(seq 1 60); do
      if curl -fsS -o /dev/null "http://localhost:${PUBLIC_PORT}/"; then
        echo " OK (200)"; exit 0
      fi
      echo -n "."; sleep 5
    done
    echo " TIMEOUT"
    echo "   Containers still booting? Check:  deploy/jnpa-uc3.sh ps  /  logs web  /  logs gateway"
    exit 1
    ;;

  backup)
    ts="$(date -u +%Y%m%dT%H%M%SZ)"
    out="backups/${ts}"
    mkdir -p "$out"
    echo ">> Dumping Postgres -> ${out}/postgres.sql.gz"
    compose exec -T postgres pg_dump -U postgres postgres | gzip > "${out}/postgres.sql.gz"
    echo ">> Snapshotting MinIO data volume -> ${out}/minio-data.tar.gz"
    # mc mirror would need creds; a volume tar is simpler and self-contained.
    docker run --rm \
      -v jnpa-uc3-poc_minio-data:/data:ro \
      -v "$ROOT_DIR/${out}:/backup" \
      alpine sh -c "tar czf /backup/minio-data.tar.gz -C /data ." || \
      echo "   (minio volume name differs? list with: docker volume ls | grep minio)"
    echo ">> Backup written to ${out}/"
    echo "   NOTE: the durable copy is the EBS snapshot (see the AWS guide). This is a logical export."
    ;;

  help|*)
    sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
    ;;
esac
