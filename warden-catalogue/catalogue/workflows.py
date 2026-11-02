"""Ingestion workflows: one Postgres service per Warden database, metadata only.

No profiler and no sample data: the catalogue holds table and column names,
types, descriptions (from the Postgres comments the owning code writes) and
tags, never rows. invoice_inbox is the only table taken from the backend
database; run state stays out, as it does for the data-quality role."""
from . import config

DATABASES = {
    # service name -> (database, tables to include; None means all in public)
    "warden_ledger": ("warden_ledger", None),
    "warden_backend": ("warden_backend", ["^invoice_inbox$"]),
}


def workflow(service: str, bot_jwt: str) -> dict:
    database, tables = DATABASES[service]
    source_config = {
        "type": "DatabaseMetadata",
        "schemaFilterPattern": {"includes": ["^public$"]},
        "includeStoredProcedures": False,
        "markDeletedTables": True,
    }
    if tables:
        source_config["tableFilterPattern"] = {"includes": tables}
    return {
        "source": {
            "type": "postgres",
            "serviceName": service,
            "serviceConnection": {"config": {
                "type": "Postgres",
                "username": config.READER,
                "authType": {"password": config.READER_PASSWORD},
                "hostPort": config.POSTGRES_HOST_PORT,
                "database": database,
                "ingestAllDatabases": False,
                "sslMode": "disable",
            }},
            "sourceConfig": {"config": source_config},
        },
        "sink": {"type": "metadata-rest", "config": {}},
        "workflowConfig": {
            "loggerLevel": "INFO",
            "openMetadataServerConfig": {
                "hostPort": f"{config.SERVER}/api",
                "authProvider": "openmetadata",
                "securityConfig": {"jwtToken": bot_jwt},
            },
        },
    }
