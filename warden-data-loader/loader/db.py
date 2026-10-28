import os
import re

import psycopg
from psycopg.rows import dict_row

INBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS invoice_inbox (
    id            BIGSERIAL PRIMARY KEY,
    source        TEXT NOT NULL CHECK (source IN ('sroie','cord')),
    external_key  TEXT NOT NULL UNIQUE,
    split         TEXT NOT NULL,
    text          TEXT NOT NULL,
    ground_truth  JSONB NOT NULL,
    loaded_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def _dsn(role: str, password_env: str) -> str:
    return (f"postgresql://{role}:{os.environ[password_env]}@{os.environ.get('POSTGRES_HOST', 'postgres')}:"
            f"{os.environ.get('POSTGRES_PORT', '5432')}/{role}")


def backend_conn():
    return psycopg.connect(_dsn("warden_backend", "PGPASS_WARDEN_BACKEND"), row_factory=dict_row)


def ledger_conn():
    return psycopg.connect(_dsn("warden_ledger", "PGPASS_WARDEN_LEDGER"), row_factory=dict_row)


def normalize_vendor(name: str) -> str:
    # Must match warden-ledger's app/db.py so lookups find seeded vendors.
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ]", " ", name.upper())).strip()
