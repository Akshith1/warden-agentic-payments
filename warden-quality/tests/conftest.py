"""Real Postgres, real Great Expectations, a real read-only role.

Needs a superuser connection from the usual libpq variables (PGHOST, PGPORT,
PGUSER or POSTGRES_USER, PGPASSWORD). Creates two throwaway databases with the
ledger's and the loader's own schema, so the checks run against the tables
they will meet in production, and a reader role with SELECT only."""
import importlib.util
import os
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[2]
LEDGER_DB, BACKEND_DB, EMPTY_DB = "quality_test_ledger", "quality_test_backend", "quality_test_empty"
READER, READER_PASSWORD = "quality_test_reader", "quality-test-reader"


def _module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem + "_schema", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ledger_db = _module(ROOT / "warden-ledger" / "app" / "db.py")
loader_db = _module(ROOT / "warden-data-loader" / "loader" / "db.py")


def admin(dbname="postgres"):
    return psycopg.connect(host=os.environ.get("PGHOST", os.environ.get("POSTGRES_HOST", "localhost")),
                           port=os.environ.get("PGPORT", os.environ.get("POSTGRES_PORT", "5432")),
                           user=os.environ.get("PGUSER", os.environ.get("POSTGRES_USER", "postgres")),
                           password=os.environ.get("PGPASSWORD"), dbname=dbname, autocommit=True)


@pytest.fixture(scope="session", autouse=True)
def databases():
    with admin() as c:
        for db in (LEDGER_DB, BACKEND_DB, EMPTY_DB):
            c.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
            c.execute(f"CREATE DATABASE {db}")
        c.execute(f"DROP ROLE IF EXISTS {READER}")
        c.execute(f"CREATE ROLE {READER} LOGIN PASSWORD '{READER_PASSWORD}'")
    with admin(LEDGER_DB) as c:
        ledger_db.init_schema(c)
        c.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {READER}")
    with admin(BACKEND_DB) as c:
        c.execute(loader_db.INBOX_SCHEMA)
        c.execute(f"GRANT SELECT ON invoice_inbox TO {READER}")
        c.execute("CREATE TABLE checkpoints (thread_id text, data text)")   # what the reader must NOT see
    os.environ.update(QUALITY_DB_USER=READER, PGPASS_WARDEN_QUALITY=READER_PASSWORD, LEDGER_DB=LEDGER_DB,
                      BACKEND_DB=BACKEND_DB, QUALITY_INTERVAL_MINUTES="0")
    from app import config
    config.DB_USER, config.DB_PASSWORD = READER, READER_PASSWORD
    config.LEDGER_DB, config.BACKEND_DB = LEDGER_DB, BACKEND_DB
    config.POSTGRES_HOST = os.environ.get("PGHOST", os.environ.get("POSTGRES_HOST", "localhost"))
    config.POSTGRES_PORT = os.environ.get("PGPORT", os.environ.get("POSTGRES_PORT", "5432"))
    config.INTERVAL_MINUTES = 0
    yield
    with admin() as c:
        for db in (LEDGER_DB, BACKEND_DB, EMPTY_DB):
            c.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        c.execute(f"DROP ROLE IF EXISTS {READER}")


def pay(c, key, vendor_id, amount, *, history=True):
    """A payment exactly as the ledger and the payment agent write one."""
    txn = c.execute("INSERT INTO transactions (idempotency_key, description) VALUES (%s, 'test') RETURNING id",
                    (key,)).fetchone()[0]
    c.execute("INSERT INTO postings (transaction_id, account_id, direction, amount) VALUES "
              "(%s, '5000-vendor-expense', 'debit', %s), (%s, '1000-operating-cash', 'credit', %s)",
              (txn, amount, txn, amount))
    c.execute("UPDATE accounts SET balance = balance + %s WHERE id = '5000-vendor-expense'", (amount,))
    c.execute("UPDATE accounts SET balance = balance - %s WHERE id = '1000-operating-cash'", (amount,))
    if history:
        c.execute("INSERT INTO historical_payments (vendor_id, amount, paid_on, source, external_ref, recorded_at) "
                  "VALUES (%s, %s, current_date, 'warden', %s, now())", (vendor_id, amount, key))
    return txn


@pytest.fixture()
def clean():
    """A small, healthy ledger and inbox. Tests break one thing each."""
    with admin(LEDGER_DB) as c:
        c.execute("TRUNCATE postings, transactions, historical_payments, vendor_aliases, vendors "
                  "RESTART IDENTITY CASCADE")
        c.execute("UPDATE accounts SET balance = 0")
        txn = c.execute("INSERT INTO transactions (idempotency_key, description) "
                        "VALUES ('opening-balance', 'Opening cash balance') RETURNING id").fetchone()[0]
        c.execute("INSERT INTO postings (transaction_id, account_id, direction, amount) VALUES "
                  "(%s, '1000-operating-cash', 'debit', 1000), (%s, '3000-opening-equity', 'credit', 1000)",
                  (txn, txn))
        c.execute("UPDATE accounts SET balance = 1000 WHERE id = '1000-operating-cash'")
        c.execute("UPDATE accounts SET balance = -1000 WHERE id = '3000-opening-equity'")
        for i in range(12):
            c.execute("INSERT INTO vendors (normalized_name, display_name, first_seen) VALUES (%s, %s, '2025-01-01')",
                      (f"VENDOR {i} SDN BHD", f"Vendor {i} Sdn Bhd"))
        c.execute("INSERT INTO historical_payments (vendor_id, amount, paid_on, source, external_ref) "
                  "VALUES (1, 1200.00, '2025-03-01', 'usaspending', 'award-1')")
        pay(c, "VENDOR 1 SDN BHD|INV-1", 2, 45)
        pay(c, "VENDOR 1 SDN BHD|INV-2", 2, 90)
    with admin(BACKEND_DB) as c:
        c.execute("TRUNCATE invoice_inbox RESTART IDENTITY")
        for i in range(30):
            split = "test" if i < 22 else "train"
            c.execute("INSERT INTO invoice_inbox (source, external_key, split, text, ground_truth) "
                      "VALUES ('sroie', %s, %s, %s, %s)",
                      (f"s-{i}", split, f"SHOP {i}\nTOTAL 12.00\nThank you, Siti", '{"entities": {"total": "12.00"}}'))
    return {"ledger": lambda: admin(LEDGER_DB), "backend": lambda: admin(BACKEND_DB)}


@pytest.fixture()
def events(monkeypatch):
    """What the service sends to the control plane's audit log."""
    from app import runner
    sent = []

    class Ok:
        status_code = 201

    monkeypatch.setattr(runner.httpx, "post", lambda url, json, timeout: sent.append(json) or Ok())
    monkeypatch.setattr(runner, "_latest", None)
    return sent
