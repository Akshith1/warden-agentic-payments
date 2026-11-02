import os
import re
import time
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
-- Other names a person has confirmed belong to an existing vendor. Never
-- created by fuzzy matching: look-alike names are a classic payment fraud.
CREATE TABLE IF NOT EXISTS vendor_aliases (
    normalized_alias  TEXT PRIMARY KEY,
    vendor_id         INT NOT NULL REFERENCES vendors(id),
    display_alias     TEXT NOT NULL,
    confirmed_by      TEXT NOT NULL,
    run_ref           TEXT,
    confirmed_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- When a payment was recorded, for the policy service's rolling 24-hour count.
-- Nullable on purpose: rows from before phase 4 and bulk-loaded history are not "recent".
ALTER TABLE historical_payments ADD COLUMN IF NOT EXISTS recorded_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS historical_payments_vendor_idx ON historical_payments (vendor_id, paid_on DESC);

-- Descriptions for the data catalogue (phase 7b), kept with the schema they describe.
COMMENT ON TABLE accounts IS 'Chart of accounts. balance is the stored running sum of postings (debits minus credits), checked before every payment.';
COMMENT ON COLUMN accounts.allow_negative IS 'False for operating cash: the ledger refuses a payment that would overdraw it.';
COMMENT ON TABLE transactions IS 'One row per double-entry transaction. idempotency_key makes a payment happen at most once.';
COMMENT ON COLUMN transactions.idempotency_key IS 'Vendor ledger name and invoice number for a payment; the same invoice twice collides here.';
COMMENT ON TABLE postings IS 'The debit and credit lines of each transaction; they must balance.';
COMMENT ON TABLE vendors IS 'The vendor master: USAspending reference vendors plus vendors Warden has paid.';
COMMENT ON COLUMN vendors.normalized_name IS 'Upper case, letters, digits and single spaces: the key every lookup uses.';
COMMENT ON TABLE historical_payments IS 'Payment history per vendor. Two settled Warden payments let an invoice skip a person, so every warden row must match a ledger payment.';
COMMENT ON COLUMN historical_payments.source IS 'usaspending: reference history loaded once. warden: written after a settled ledger payment.';
COMMENT ON COLUMN historical_payments.external_ref IS 'For source warden, the idempotency_key of the ledger transaction that paid it.';
COMMENT ON COLUMN historical_payments.remit_account IS 'The account paid. Personal data when the payee is a sole trader.';
COMMENT ON COLUMN historical_payments.recorded_at IS 'When the payment was recorded, for the policy service''s 24-hour count.';
COMMENT ON TABLE vendor_aliases IS 'Other names a person confirmed belong to an existing vendor. Never created by fuzzy matching.';
COMMENT ON COLUMN vendor_aliases.confirmed_by IS 'Username of the person who confirmed the link.';
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


def wait_for_db(timeout: float = float(os.environ.get("DB_WAIT_SECONDS", "60"))) -> None:
    # Postgres may still be initializing when this container starts; retry instead of crash-looping.
    deadline = time.monotonic() + timeout
    while True:
        try:
            psycopg.connect(dsn(), connect_timeout=3).close()
            return
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                raise
            time.sleep(2)


def init_schema(conn) -> None:
    conn.execute(SCHEMA)
    for acc_id, name, acc_type, allow_negative in CHART_OF_ACCOUNTS:
        conn.execute(
            "INSERT INTO accounts (id, name, type, allow_negative) VALUES (%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
            (acc_id, name, acc_type, allow_negative),
        )


def normalize_vendor(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ]", " ", name.upper())).strip()


def resolve_vendor(conn, name: str):
    """The vendor a name belongs to: its own name first, then a confirmed alias.
    Returns the vendor row plus `matched_alias` (None for a direct match), or None."""
    key = normalize_vendor(name)
    row = conn.execute("SELECT id, display_name, first_seen, NULL::text AS matched_alias "
                       "FROM vendors WHERE normalized_name = %s", (key,)).fetchone()
    if row:
        return row
    return conn.execute(
        """SELECT v.id, v.display_name, v.first_seen, a.display_alias AS matched_alias
           FROM vendor_aliases a JOIN vendors v ON v.id = a.vendor_id WHERE a.normalized_alias = %s""",
        (key,),
    ).fetchone()
