#!/bin/sh
# One database and one login role per service, so no service can read
# another's tables. Postgres runs this automatically on the first start of an
# empty data volume. It is idempotent, so an existing install can pick up a
# database added by a later phase without losing data:
#   compose up -d postgres            (recreates the container with any new PGPASS_* vars)
#   compose exec postgres sh /docker-entrypoint-initdb.d/01-create-service-databases.sh
set -eu

create() {
  role="$1"; password="$2"
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<SQL
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', '$role', '$password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$role') \gexec
SELECT format('CREATE DATABASE %I OWNER %I', '$role', '$role')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = '$role') \gexec
REVOKE ALL ON DATABASE $role FROM PUBLIC;
SQL
}

create warden_ledger "${PGPASS_WARDEN_LEDGER:?}"
create warden_backend "${PGPASS_WARDEN_BACKEND:?}"
create litellm "${PGPASS_LITELLM:?}"
create mlflow "${PGPASS_MLFLOW:?add PGPASS_MLFLOW to .env (phase 2)}"
