#!/usr/bin/env bash
# JNPA UC3 — convenience wrapper for the AWS compose stack. Run from the repo
# root on the EC2 box. Saves typing the two -f flags every time.
#
#   ./deploy/aws/manage.sh up        # build + start (detached)
#   ./deploy/aws/manage.sh down      # stop + remove containers (keeps volumes)
#   ./deploy/aws/manage.sh restart   # restart all services
#   ./deploy/aws/manage.sh logs      # follow gateway + web logs
#   ./deploy/aws/manage.sh ps        # status
#   ./deploy/aws/manage.sh update    # git pull + rebuild changed images + up
#   ./deploy/aws/manage.sh nuke      # down + remove volumes (DESTROYS minio etc.)
#
# DATABASE: AWS RDS only (jnpa_schema_v3). The env file (default .env, override
# with ENV_FILE=…) must define POSTGRES_DSN + the four libpq DSNs; compose uses
# ${VAR:?…} so a missing one aborts instead of falling back to a local database.
#   RDS_PASSWORD=… bash deploy/aws/use-rds.sh .env     # write the RDS values
#   bash deploy/aws/verify-rds.sh .env                 # prove RDS-only
set -euo pipefail
cd "$(dirname "$0")/../.."
ENV_FILE="${ENV_FILE:-.env}"
DC=(docker compose --env-file "$ENV_FILE" -f docker-compose.yml -f docker-compose.aws.yml)

case "${1:-up}" in
  up)       "${DC[@]}" up -d --build ;;
  down)     "${DC[@]}" down ;;
  restart)  "${DC[@]}" restart ;;
  logs)     "${DC[@]}" logs -f gateway web ;;
  ps)       "${DC[@]}" ps ;;
  update)   git pull --ff-only && "${DC[@]}" up -d --build ;;
  nuke)     "${DC[@]}" down -v ;;
  *)        echo "usage: $0 {up|down|restart|logs|ps|update|nuke}"; exit 1 ;;
esac
