"""Shared setup for the integration tests. Env must be set before any app
module is imported, because app.config reads it at import time."""
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

os.environ.update({"LEDGER_WRITE_KEY": "t-write", "VENDOR_LOOKUP_KEY": "t-read", "OPENING_CASH": "10000.00"})
LEDGER_PORT = 18001

from langgraph.checkpoint.postgres import PostgresSaver  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402
from psycopg_pool import ConnectionPool  # noqa: E402

from app import config  # noqa: E402
from app.ledger_client import LedgerClient  # noqa: E402

LEDGER_DIR = Path(__file__).resolve().parents[2] / "warden-ledger"


@pytest.fixture(scope="session")
def ledger_url():
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(LEDGER_PORT)],
                            cwd=LEDGER_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{LEDGER_PORT}"
    for _ in range(50):
        try:
            if httpx.get(f"{url}/health").status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    yield url
    proc.terminate()


@pytest.fixture(scope="session")
def checkpointer():
    pool = ConnectionPool(config.backend_dsn(), max_size=4, open=True,
                          kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})
    saver = PostgresSaver(pool)
    saver.setup()
    yield saver
    pool.close()


@pytest.fixture()
def ledger(ledger_url):
    import psycopg
    dsn = (f"postgresql://warden_ledger:{os.environ['PGPASS_WARDEN_LEDGER']}@{os.environ['POSTGRES_HOST']}:"
           f"{os.environ['POSTGRES_PORT']}/warden_ledger")
    with psycopg.connect(dsn) as conn:
        conn.execute("TRUNCATE postings, transactions, historical_payments, vendors RESTART IDENTITY CASCADE")
        conn.execute("UPDATE accounts SET balance = 0")
        conn.execute("INSERT INTO transactions (idempotency_key, description) VALUES ('opening-balance', 'open')")
        conn.execute("INSERT INTO postings (transaction_id, account_id, direction, amount) VALUES "
                     "(1, '1000-operating-cash', 'debit', 10000), (1, '3000-opening-equity', 'credit', 10000)")
        conn.execute("UPDATE accounts SET balance = 10000 WHERE id = '1000-operating-cash'")
        conn.execute("UPDATE accounts SET balance = -10000 WHERE id = '3000-opening-equity'")
    return LedgerClient(ledger_url)


