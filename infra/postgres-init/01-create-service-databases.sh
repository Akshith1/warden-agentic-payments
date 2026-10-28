#!/bin/sh
# One database and one login role per service, so no service can read
# another's tables. Runs once, on the first start of an empty data volume.
set -eu

create() {
  role="$1"; password="$2"
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<SQL
CREATE ROLE $role LOGIN PASSWORD '$password';
CREATE DATABASE $role OWNER $role;
REVOKE ALL ON DATABASE $role FROM PUBLIC;
SQL
}

create warden_ledger "$PGPASS_WARDEN_LEDGER"
create warden_backend "$PGPASS_WARDEN_BACKEND"
create litellm "$PGPASS_LITELLM"
