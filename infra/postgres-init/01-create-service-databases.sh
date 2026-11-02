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

# Phase 7b: the data-quality service reads, never writes. It may read every ledger table
# and, in the backend database, only invoice_inbox: run state (LangGraph checkpoints) holds
# unmasked invoices and stays out of reach. The loader repeats the inbox grant when it
# creates the table, so the order of first start and first load doesn't matter.
reader() {
  role="$1"; password="$2"
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<SQL
SELECT format('CREATE ROLE %I LOGIN', '$role')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$role') \gexec
ALTER ROLE $role LOGIN PASSWORD '$password' NOSUPERUSER NOCREATEDB NOCREATEROLE;
ALTER ROLE $role SET default_transaction_read_only = on;
GRANT CONNECT ON DATABASE warden_ledger TO $role;
GRANT CONNECT ON DATABASE warden_backend TO $role;
\c warden_ledger
GRANT USAGE ON SCHEMA public TO $role;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO $role;
ALTER DEFAULT PRIVILEGES FOR ROLE warden_ledger IN SCHEMA public GRANT SELECT ON TABLES TO $role;
\c warden_backend
GRANT USAGE ON SCHEMA public TO $role;
SELECT format('GRANT SELECT ON invoice_inbox TO %I', '$role')
WHERE to_regclass('public.invoice_inbox') IS NOT NULL \gexec
SQL
}

reader warden_quality "${PGPASS_WARDEN_QUALITY:?add PGPASS_WARDEN_QUALITY to .env (phase 7b)}"
