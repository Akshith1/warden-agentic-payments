#!/bin/sh
# Phase 11: the Azure counterpart of infra/postgres-init. Runs once per deploy as a Container
# Apps job (warden-db-init), connecting to the Flexible Server as its admin over TLS (PGHOST,
# PGUSER, PGPASSWORD, PGSSLMODE=require come from the job). One database and one login role per
# service, as locally. Idempotent: it also resets each role's password to the one in Key Vault,
# so a rotated secret takes effect on the next deploy.
#
# The admin of a Flexible Server is not a superuser (it is a member of azure_pg_admin). It may create roles, and gets the right
# to grant each role it creates; it grants the role to itself so it may make it a database owner.
set -eu

create() {
  role="$1"; password="$2"
  psql -v ON_ERROR_STOP=1 --dbname postgres <<SQL
SELECT format('CREATE ROLE %I LOGIN', '$role')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$role') \gexec
ALTER ROLE $role LOGIN PASSWORD '$password' NOCREATEDB NOCREATEROLE;
SELECT format('GRANT %I TO %I', '$role', current_user)
WHERE NOT pg_has_role(current_user, '$role', 'SET') \gexec
SELECT format('CREATE DATABASE %I OWNER %I', '$role', '$role')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = '$role') \gexec
REVOKE ALL ON DATABASE $role FROM PUBLIC;
SQL
  # On Flexible Server the public schema of a new database belongs to azure_pg_admin, not to
  # the database's owner, so the service couldn't create its tables. Give it the schema.
  psql -v ON_ERROR_STOP=1 --dbname "$role" -c "ALTER SCHEMA public OWNER TO $role"
  echo "ready: database and role $role"
}

create warden_ledger "${PGPASS_WARDEN_LEDGER:?}"
create warden_backend "${PGPASS_WARDEN_BACKEND:?}"
create warden_control "${PGPASS_WARDEN_CONTROL:?}"   # phase 11: the audit log and kill switch
