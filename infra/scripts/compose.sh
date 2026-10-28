#!/bin/sh
# Runs docker compose with every layer file applied, from the repo root, so
# day-to-day commands stay short:  sh infra/scripts/compose.sh up -d
# Layers added in later phases (identity, observability) join this list.
set -eu
cd "$(dirname "$0")/../.."
exec docker compose --project-name warden --project-directory . --env-file .env \
  -f infra/compose/docker-compose.base.yml \
  -f infra/compose/docker-compose.model.yml \
  -f infra/compose/docker-compose.data.yml \
  -f infra/compose/docker-compose.app.yml \
  "$@"
