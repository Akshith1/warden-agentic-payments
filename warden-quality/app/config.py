import os
from urllib.parse import quote

# A login role that may only read: SELECT on the ledger's tables and on invoice_inbox, nothing else
# (see infra/postgres-init). Every session is also opened read-only, so even a mistaken grant
# can't let a check write.
DB_USER = os.environ.get("QUALITY_DB_USER", "warden_quality")
DB_PASSWORD = os.environ.get("PGPASS_WARDEN_QUALITY", "")
POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "postgres")
POSTGRES_PORT = os.environ.get("POSTGRES_PORT", "5432")
LEDGER_DB = os.environ.get("LEDGER_DB", "warden_ledger")
BACKEND_DB = os.environ.get("BACKEND_DB", "warden_backend")


def dsn(database: str) -> str:
    return (f"postgresql+psycopg://{quote(DB_USER)}:{quote(DB_PASSWORD, safe='')}@{POSTGRES_HOST}:{POSTGRES_PORT}"
            f"/{database}?options=-c%20default_transaction_read_only%3Don")


CONTROL_URL = os.environ.get("CONTROL_URL", "http://warden-control:8000")
# How often the checks run by themselves. "Check now" in the console and the data loader run them too.
INTERVAL_MINUTES = float(os.environ.get("QUALITY_INTERVAL_MINUTES", "10"))
FIRST_RUN_DELAY_SECONDS = float(os.environ.get("QUALITY_FIRST_RUN_DELAY_SECONDS", "20"))

KEYCLOAK_ISSUER = os.environ.get("KEYCLOAK_ISSUER", "http://localhost:8180/realms/warden")
KEYCLOAK_JWKS_URL = os.environ.get("KEYCLOAK_JWKS_URL",
                                   "http://keycloak:8080/realms/warden/protocol/openid-connect/certs")
TOKEN_AUDIENCE = os.environ.get("TOKEN_AUDIENCE", "warden-backend")
