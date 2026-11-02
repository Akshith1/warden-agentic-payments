"""Shared setup for the integration tests. Env must be set before any app
module is imported, because app.config reads it at import time."""
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

LEDGER_PORT, POLICY_PORT, CONTROL_PORT, PII_PORT = 18001, 18002, 18003, 18004
os.environ.update({"LEDGER_WRITE_KEY": "t-write", "VENDOR_LOOKUP_KEY": "t-read", "OPENING_CASH": "10000.00",
                   "POLICY_URL": f"http://127.0.0.1:{POLICY_PORT}",
                   "CONTROL_URL": f"http://127.0.0.1:{CONTROL_PORT}",
                   "PII_URL": f"http://127.0.0.1:{PII_PORT}"})

from langgraph.checkpoint.postgres import PostgresSaver  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402
from psycopg_pool import ConnectionPool  # noqa: E402

from app import config  # noqa: E402
from app.ledger_client import LedgerClient  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
LEDGER_DIR = ROOT / "warden-ledger"
POLICY_DIR = ROOT / "warden-policy"
CONTROL_DIR = ROOT / "warden-control"


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


@pytest.fixture(scope="session")
def control_url():
    # The real control plane: kill switch and audit log, on a throwaway SQLite file. The
    # repeated-refusal trigger is raised out of reach here; warden-control tests it itself.
    if CONTROL_DB.exists():
        CONTROL_DB.unlink()
    proc, url = serve(CONTROL_DIR, CONTROL_PORT, {"CONTROL_DB": str(CONTROL_DB), "DENIAL_LIMIT": "100000"})
    yield url
    proc.terminate()


CONTROL_DB = Path(os.environ.get("TMPDIR", "/tmp")) / f"warden-test-control-{os.getpid()}.db"


class Switch:
    """Flips the kill switch the way the console does, straight in the control plane's store."""
    def __init__(self, db: Path):
        self.db = db

    def _sql(self, sql, *args):
        import sqlite3
        c = sqlite3.connect(self.db, isolation_level=None)
        c.execute(sql, args)
        c.close()

    def trip(self, scope="global", target="*", reason="test stop"):
        self._sql("INSERT OR IGNORE INTO switches VALUES (?, ?, ?, 'tester', datetime('now'))", scope, target, reason)

    def reset(self):
        self._sql("DELETE FROM switches")


@pytest.fixture()
def switch(control_url):
    s = Switch(CONTROL_DB)
    s.reset()
    yield s
    s.reset()


@pytest.fixture(scope="session", autouse=True)
def pii_url():
    """A stand-in for warden-pii with the same contract: emails, 'Cashier: NAME' and bank accounts
    after 'account'. Presidio itself is tested in warden-pii."""
    import json
    import re
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    rules = [("EMAIL_ADDRESS", r"[\w.+-]+@[\w-]+\.[\w.]+"), ("PERSON", r"(?<=Cashier: )[A-Z][A-Za-z ]+[A-Za-z]"),
             ("BANK_ACCOUNT", r"(?<=account )\d[\d-]{6,}\d")]

    class Mask(BaseHTTPRequestHandler):
        def do_POST(self):
            text = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["text"]
            mapping, counts = {}, {}
            for kind, pattern in rules:
                for value in dict.fromkeys(re.findall(pattern, text)):
                    counts[kind] = counts.get(kind, 0) + 1
                    token = f"<{kind}_{counts[kind]}>"
                    mapping[token] = value
                    text = text.replace(value, token)
            body = json.dumps({"masked": text, "mapping": mapping, "counts": counts}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", PII_PORT), Mask)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{PII_PORT}"
    server.shutdown()


@pytest.fixture(scope="session", autouse=True)
def policy_url(ledger_url, control_url):
    # The real Cedar policy service, reading vendor history from the test ledger.
    proc, url = serve(POLICY_DIR, POLICY_PORT, {"LEDGER_URL": ledger_url, "POLICY_DIR": str(ROOT / "policies"),
                                                "CONTROL_URL": control_url})
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


