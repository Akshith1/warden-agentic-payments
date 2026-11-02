"""Shared setup for the integration tests. Env must be set before any app
module is imported, because app.config reads it at import time."""
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

LEDGER_PORT, POLICY_PORT = 18001, 18002
os.environ.update({"LEDGER_WRITE_KEY": "t-write", "VENDOR_LOOKUP_KEY": "t-read", "OPENING_CASH": "10000.00",
                   "POLICY_URL": f"http://127.0.0.1:{POLICY_PORT}"})

from langgraph.checkpoint.postgres import PostgresSaver  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402
from psycopg_pool import ConnectionPool  # noqa: E402

from app import config  # noqa: E402
from app.ledger_client import LedgerClient  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
LEDGER_DIR = ROOT / "warden-ledger"
POLICY_DIR = ROOT / "warden-policy"


def serve(cwd: Path, port: int, env: dict | None = None):
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)], cwd=cwd,
                            env=os.environ | (env or {}), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    for _ in range(75):
        try:
            if httpx.get(f"{url}/health").status_code == 200:
                return proc, url
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    proc.terminate()
    raise RuntimeError(f"{cwd.name} did not start on {port}")


@pytest.fixture(scope="session")
def ledger_url():
    proc, url = serve(LEDGER_DIR, LEDGER_PORT)
    yield url
    proc.terminate()


@pytest.fixture(scope="session", autouse=True)
def policy_url(ledger_url):
    # The real Cedar policy service, reading vendor history from the test ledger.
    proc, url = serve(POLICY_DIR, POLICY_PORT, {"LEDGER_URL": ledger_url, "POLICY_DIR": str(ROOT / "policies")})
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
        conn.execute("TRUNCATE postings, transactions RESTART IDENTITY CASCADE")
        # Vendor ids keep counting up across tests: the policy service remembers recent
        # approvals by vendor id for the whole session, so a reused id would carry them over.
        conn.execute("TRUNCATE historical_payments, vendors CASCADE")
        conn.execute("UPDATE accounts SET balance = 0")
        conn.execute("INSERT INTO transactions (idempotency_key, description) VALUES ('opening-balance', 'open')")
        conn.execute("INSERT INTO postings (transaction_id, account_id, direction, amount) VALUES "
                     "(1, '1000-operating-cash', 'debit', 10000), (1, '3000-opening-equity', 'credit', 10000)")
        conn.execute("UPDATE accounts SET balance = 10000 WHERE id = '1000-operating-cash'")
        conn.execute("UPDATE accounts SET balance = -10000 WHERE id = '3000-opening-equity'")
    return LedgerClient(ledger_url)


