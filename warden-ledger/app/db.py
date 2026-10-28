import os
import re
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    type            TEXT NOT NULL CHECK (type IN ('asset','liability','equity','revenue','expense')),
    allow_negative  BOOLEAN NOT NULL DEFAULT TRUE,
    -- Signed debit-minus-credit balance; asset/expense accounts are normally positive.
    balance         NUMERIC(18,2) NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS transactions (
    id               BIGSERIAL PRIMARY KEY,
    idempotency_key  TEXT NOT NULL UNIQUE,
    description      TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS postings (
    id              BIGSERIAL PRIMARY KEY,
    transaction_id  BIGINT NOT NULL REFERENCES transactions(id),
    account_id      TEXT NOT NULL REFERENCES accounts(id),
    direction       TEXT NOT NULL CHECK (direction IN ('debit','credit')),
    amount          NUMERIC(18,2) NOT NULL CHECK (amount > 0)
);
CREATE TABLE IF NOT EXISTS vendors (
    id                 SERIAL PRIMARY KEY,
    normalized_name    TEXT NOT NULL UNIQUE,
    display_name       TEXT NOT NULL,
    first_seen         DATE NOT NULL
);
CREATE TABLE IF NOT EXISTS historical_payments (
    id             BIGSERIAL PRIMARY KEY,
    vendor_id      INT NOT NULL REFERENCES vendors(id),
    amount         NUMERIC(18,2) NOT NULL,
    paid_on        DATE NOT NULL,
    source         TEXT NOT NULL CHECK (source IN ('usaspending','warden')),
    external_ref   TEXT NOT NULL UNIQUE,
    remit_account  TEXT
);
CREATE INDEX IF NOT EXISTS historical_payments_vendor_idx ON historical_payments (vendor_id, paid_on DESC);
"""

CHART_OF_ACCOUNTS = [
    ("1000-operating-cash", "Operating cash", "asset", False),
    ("2000-accounts-payable", "Accounts payable", "liability", True),
    ("3000-opening-equity", "Opening equity", "equity", True),
    ("5000-vendor-expense", "Vendor expense", "expense", True),
]


def dsn() -> str:
    return (
        f"postgresql://warden_ledger:{os.environ['PGPASS_WARDEN_LEDGER']}"
        f"@{os.environ.get('POSTGRES_HOST', 'postgres')}:{os.environ.get('POSTGRES_PORT', '5432')}"
        f"/{os.environ.get('LEDGER_DB', 'warden_ledger')}"
    )


@contextmanager
def connect():
    with psycopg.connect(dsn(), row_factory=dict_row) as conn:
        yield conn


def init_schema(conn) -> None:
    conn.execute(SCHEMA)
    for acc_id, name, acc_type, allow_negative in CHART_OF_ACCOUNTS:
        conn.execute(
            "INSERT INTO accounts (id, name, type, allow_negative) VALUES (%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
            (acc_id, name, acc_type, allow_negative),
        )


def normalize_vendor(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ]", " ", name.upper())).strip()
