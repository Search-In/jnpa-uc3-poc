#!/usr/bin/env bash
# =============================================================================
# JNPA UC3 — convenience wrapper for the QA compose stack, mirroring
# deploy/aws/manage.sh. Run from the QA checkout on the EC2 box.
#
#   ./deploy/qa/manage.sh up        # build + start QA (detached)
#   ./deploy/qa/manage.sh down      # stop + remove QA containers (keeps volumes)
#   ./deploy/qa/manage.sh restart   # restart QA services
#   ./deploy/qa/manage.sh logs      # follow QA gateway + web logs
#   ./deploy/qa/manage.sh ps        # QA status
#   ./deploy/qa/manage.sh update    # git pull + rebuild changed QA images + up
#   ./deploy/qa/manage.sh config    # render the merged QA config (no side effects)
#
# EVERY command here is scoped to the `jnpa-uc3-poc-qa` compose project and puts
# docker-compose.qa.yml LAST in the -f chain, so it can never stop, rebuild or
# reconfigure a production container. `nuke` is deliberately NOT provided, and
# there is no code path that runs an unscoped `docker compose down`.
#
# DATABASE: AWS RDS, database `jnpa_qa` (production is jnpa_schema_v3). The env
# file (default .env.qa, override with ENV_FILE=…) must define POSTGRES_DSN +
# the four libpq DSNs; compose uses ${VAR:?…} so a missing one aborts instead of
# falling back to a local database. Write them with:
#
#   RDS_HOST=<the RDS endpoint — not committed, see docs/RDS_SECURITY.md> \
#   RDS_DB=jnpa_qa RDS_USER=jnpa_app RDS_PASSWORD='…' \
#     bash deploy/aws/use-rds.sh .env.qa
#   bash deploy/qa/verify-qa.sh          # prove QA -> jnpa_qa, prod untouched
#
# Full runbook: QA_DEPLOYMENT.md
set -euo pipefail
cd "$(dirname "$0")/../.."

ENV_FILE="${ENV_FILE:-.env.qa}"
PROJECT="${QA_PROJECT:-jnpa-uc3-poc-qa}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found." >&2
  echo "  cp .env.qa.example $ENV_FILE && chmod 600 $ENV_FILE   # then fill the <SET_ON_EC2_ONLY> values" >&2
  exit 1
fi

# Guard: refuse to run if the env file points at the PRODUCTION database. A
# copy-paste of .env into .env.qa is the one mistake that would let a QA stack
# write to production data.
if grep -qE '^[A-Z_]*POSTGRES_DSN=.*jnpa_schema_v3' "$ENV_FILE"; then
  echo "ERROR: $ENV_FILE contains a DSN pointing at the PRODUCTION database (jnpa_schema_v3)." >&2
  echo "  QA must use jnpa_qa. Refusing to start." >&2
  exit 1
fi

# Overlay order is load-bearing: docker-compose.aws.yml gives every service
# `restart: unless-stopped` and hides the internal host ports, then
# docker-compose.qa.yml must come LAST so its `!override` port/volume entries
# REPLACE the production ones (80/443 + the Let's Encrypt mount) rather than
# merging with them.
DC=(docker compose --env-file "$ENV_FILE" -p "$PROJECT"
    -f docker-compose.yml -f docker-compose.aws.yml -f docker-compose.qa.yml)

case "${1:-up}" in
  up)       "${DC[@]}" up -d --build ;;
  down)     "${DC[@]}" down ;;
  restart)  "${DC[@]}" restart ;;
  logs)     "${DC[@]}" logs -f gateway web ;;
  ps)       "${DC[@]}" ps ;;
  config)   "${DC[@]}" config ;;
  update)   git pull --ff-only && "${DC[@]}" up -d --build ;;
  *)        echo "usage: $0 {up|down|restart|logs|ps|config|update}"; exit 1 ;;
esac
