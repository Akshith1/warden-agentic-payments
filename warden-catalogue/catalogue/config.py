import os

# OpenMetadata's API, as the ingestion job reaches it on the Docker network.
SERVER = os.environ.get("CATALOGUE_URL", "http://catalogue:8585")
ADMIN_EMAIL = os.environ.get("CATALOGUE_ADMIN_EMAIL", "admin@open-metadata.org")
ADMIN_PASSWORD = os.environ.get("OPENMETADATA_ADMIN_PASSWORD", "")

# The catalogue reads Warden's databases as the data-quality role: SELECT only, on the ledger
# and invoice_inbox, nothing else. So it can describe exactly what the checks can see.
POSTGRES_HOST_PORT = os.environ.get("POSTGRES_HOST_PORT", "postgres:5432")
READER = os.environ.get("QUALITY_DB_USER", "warden_quality")
READER_PASSWORD = os.environ.get("PGPASS_WARDEN_QUALITY", "")

KEYS_DIR = os.environ.get("CATALOGUE_KEYS_DIR", "/keys")
