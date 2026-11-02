"""Each test breaks the data the way a bug, a bad load or a person with database
access could, and checks that the right expectation catches it."""
import os
import time

import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app import auth, config, runner
from app.checks import CHECKS

from .conftest import EMPTY_DB, READER, READER_PASSWORD, pay


def by_id(result):
    return {c["id"]: c for c in result["checks"]}


def test_healthy_data_passes_every_check(clean, events):
    result = runner.run_checks()
    failing = {c["id"]: c for c in result["checks"] if c["status"] != "passed"}
    assert failing == {} and result["passed"] == len(CHECKS) and result["critical_failed"] == []
    assert len(events) == 1 and events[0]["type"] == "data_quality"   # the first result is always news
    runner.run_checks()
    assert len(events) == 1   # a scheduled run that changes nothing stays out of the audit log


def test_vendor_history_with_no_payment_behind_it_is_critical(clean, events):
    # Someone with database access gives a vendor two "settled payments" so its next invoice
    # skips a person. The rows are valid one by one; only the cross-check sees it.
    with clean["ledger"]() as c:
        for n in (1, 2):
            c.execute("INSERT INTO historical_payments (vendor_id, amount, paid_on, source, external_ref, "
                      "recorded_at) VALUES (5, 49.00, current_date, 'warden', %s, now())", (f"VENDOR 4|FAKE-{n}",))
    result = runner.run_checks()
    check = by_id(result)["history-backed-by-ledger"]
    assert check["status"] == "failed" and check["count"] == 2 and check["examples"] == ["4", "5"]
    assert result["critical_failed"] == ["history-backed-by-ledger"]
    sent = events[-1]["payload"]
    assert sent["critical_failed"] == ["history-backed-by-ledger"]
    assert sent["failed_checks"][0]["title"].startswith("Every Warden payment in vendor history")


def test_an_amount_changed_in_history_is_caught(clean, events):
    with clean["ledger"]() as c:
        c.execute("UPDATE historical_payments SET amount = 4500 WHERE external_ref = 'VENDOR 1 SDN BHD|INV-1'")
    assert by_id(runner.run_checks())["history-backed-by-ledger"]["count"] == 1


def test_a_posting_written_around_the_ledger_breaks_two_checks(clean, events):
    with clean["ledger"]() as c:
        c.execute("INSERT INTO postings (transaction_id, account_id, direction, amount) "
                  "VALUES (2, '5000-vendor-expense', 'debit', 10)")
    result = by_id(runner.run_checks())
    assert result["ledger-balanced"]["status"] == "failed" and result["ledger-balanced"]["examples"] == ["2"]
    assert result["balances-match-postings"]["examples"] == ["5000-vendor-expense"]
    assert result["ledger-two-sided"]["status"] == "passed"


def test_one_sided_and_overdrawn(clean, events):
    with clean["ledger"]() as c:
        txn = c.execute("INSERT INTO transactions (idempotency_key, description) VALUES ('half', 'x') "
                        "RETURNING id").fetchone()[0]
        c.execute("INSERT INTO postings (transaction_id, account_id, direction, amount) "
                  "VALUES (%s, '1000-operating-cash', 'credit', 5000)", (txn,))
        c.execute("UPDATE accounts SET balance = balance - 5000 WHERE id = '1000-operating-cash'")
    result = by_id(runner.run_checks())
    assert result["ledger-two-sided"]["examples"] == [str(txn)]
    assert result["no-overdraft"]["examples"] == ["1000-operating-cash"]


def test_reference_and_inbox_problems_are_warnings(clean, events):
    with clean["ledger"]() as c:
        pay(c, "VENDOR 2 SDN BHD|INV-9", 3, 20, history=False)
        c.execute("INSERT INTO vendors (normalized_name, display_name, first_seen) "
                  "VALUES ('acme  trading', 'acme trading', current_date)")
        c.execute("INSERT INTO vendor_aliases (normalized_alias, vendor_id, display_alias, confirmed_by) "
                  "VALUES ('VENDOR 7 SDN BHD', 1, 'Vendor 7 Sdn Bhd', 'amira')")
        c.execute("INSERT INTO historical_payments (vendor_id, amount, paid_on, source, external_ref) "
                  "VALUES (1, 0, current_date + 3, 'usaspending', 'award-0')")
    with clean["backend"]() as c:
        c.execute("UPDATE invoice_inbox SET text = 'Siti 0123' WHERE external_key = 's-1'")
        c.execute("UPDATE invoice_inbox SET split = 'validation' WHERE external_key = 's-2'")
        c.execute("UPDATE invoice_inbox SET ground_truth = '{\"entities\": {}}' WHERE external_key = 's-3'")
        c.execute("UPDATE invoice_inbox SET split = 'train' WHERE external_key IN ('s-4', 's-5', 's-6')")
    result = runner.run_checks()
    checks = by_id(result)
    assert result["critical_failed"] == []
    expected = {"payments-in-history": ["4"], "vendor-names-normalised": ["acme  trading"],
                "alias-not-a-vendor": ["VENDOR 7 SDN BHD"], "history-amounts-positive": [],
                "history-not-in-future": ["4"], "inbox-text-length": [], "inbox-split": ["validation"],
                "inbox-labels": ["4"], "eval-set-size": ["test split"]}
    assert {i: c["examples"] for i, c in checks.items() if c["status"] == "failed"} == expected
    assert all(checks[i]["severity"] == "warning" for i in expected)
    # A receipt's text is personal data: the failing value is counted, never shown or logged.
    assert checks["inbox-text-length"]["count"] == 1
    assert "Siti" not in str(events[-1])


def test_the_reader_role_cannot_write_or_see_other_tables(clean):
    dsn = (f"host={config.POSTGRES_HOST} port={config.POSTGRES_PORT} user={READER} password={READER_PASSWORD} "
           "options='-c default_transaction_read_only=on'")
    with psycopg.connect(dsn + f" dbname={config.LEDGER_DB}") as c, pytest.raises(psycopg.Error, match="read-only"):
        c.execute("UPDATE accounts SET balance = 0")
    with psycopg.connect(dsn.replace("options='-c default_transaction_read_only=on'", "") +
                         f" dbname={config.LEDGER_DB}") as c, pytest.raises(psycopg.Error, match="permission denied"):
        c.execute("DELETE FROM historical_payments")   # even without the read-only session, the grant says no
    with psycopg.connect(dsn + f" dbname={config.BACKEND_DB}") as c, pytest.raises(psycopg.Error,
                                                                                  match="permission denied"):
        c.execute("SELECT * FROM checkpoints")   # run state holds unmasked invoices; the reader can't see it


def test_an_unreadable_table_is_an_error_not_a_crash(clean, events, monkeypatch):
    monkeypatch.setattr(config, "BACKEND_DB", EMPTY_DB)   # the loader has never run here
    result = runner.run_checks()
    inbox = [c for c in result["checks"] if c["suite"] == "inbox"]
    assert {c["status"] for c in inbox} == {"error"} and result["errors"] == len(inbox)
    assert by_id(result)["ledger-balanced"]["status"] == "passed"   # the ledger is still checked
    assert result["critical_failed"] == []                          # an error doesn't stop payments


KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class Keys:
    def get_signing_key_from_jwt(self, token):
        return type("K", (), {"key": KEY.public_key()})()


def bearer(username, *roles):
    now = int(time.time())
    token = jwt.encode({"iss": config.KEYCLOAK_ISSUER, "aud": "warden-backend", "sub": username, "exp": now + 300,
                        "preferred_username": username, "realm_access": {"roles": list(roles)}}, KEY,
                       algorithm="RS256")
    return {"Authorization": f"Bearer {token}"}


def test_the_api(clean, events, monkeypatch):
    from app import main
    monkeypatch.setattr(auth, "_jwks", Keys())
    with TestClient(main.app) as api:
        assert api.get("/health").json() == {"status": "ok", "checks": len(CHECKS)}
        assert api.get("/results").status_code == 401
        assert api.get("/results", headers=bearer("clerk", "submitter")).status_code == 404   # nothing run yet
        assert api.post("/run").json()["requested_by"] == "service"                         # the data loader
        ran = api.post("/run", headers=bearer("amira", "approver")).json()
        assert ran["requested_by"] == "amira" and ran["passed"] == len(CHECKS)
        assert events[-1]["actor"] == "amira"   # a person's check is always on the record
        assert api.get("/results", headers=bearer("clerk", "submitter")).json()["at"] == ran["at"]
        assert len(api.get("/checks", headers=bearer("clerk", "submitter")).json()) == len(CHECKS)
        assert api.get("/results", headers=bearer("nobody")).status_code == 403


READ_ONLY_RUN = r"""
import json, os, sys
ALLOWED = os.path.realpath(os.environ["TMPDIR"])
WRITE = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
def outside(path):
    try:
        path = os.fsdecode(path)
    except TypeError:
        return False   # a file descriptor: already opened
    return not os.path.realpath(path).startswith(ALLOWED + os.sep)
def hook(event, args):
    # As the container: the whole filesystem is read-only except the /tmp tmpfs.
    if event == "open" and args[0] is not None and outside(args[0]) and (
            (args[2] or 0) & WRITE or any(m in (args[1] or "") for m in "wax+")):
        raise PermissionError(f"read-only filesystem: {args[0]}")
    if event in ("os.mkdir", "os.rename", "os.remove", "os.rmdir", "os.symlink") and outside(args[0]):
        raise PermissionError(f"read-only filesystem: {event} {args[0]}")
sys.addaudithook(hook)
from app import config, runner
config.DB_USER, config.DB_PASSWORD = os.environ["QUALITY_DB_USER"], os.environ["PGPASS_WARDEN_QUALITY"]
config.LEDGER_DB, config.BACKEND_DB = os.environ["LEDGER_DB"], os.environ["BACKEND_DB"]
config.POSTGRES_HOST, config.POSTGRES_PORT = sys.argv[1], sys.argv[2]
config.CONTROL_URL = "http://127.0.0.1:9"   # nothing there; recording fails softly
print(json.dumps(runner.run_checks()))
"""


def test_the_checks_run_on_a_read_only_filesystem(clean, tmp_path):
    # Phase 7's live run failed because a library wrote a lock file on the PII service's read-only
    # disk. This service runs read-only too, so every check runs here with all writes refused
    # outside a temporary folder, as in the container.
    import json
    import subprocess
    import sys
    from pathlib import Path
    home = tmp_path / "home-not-writable"
    env = {k: v for k, v in os.environ.items()} | {"TMPDIR": str(tmp_path), "HOME": str(home),
                                                     "GX_ANALYTICS_ENABLED": "False"}
    out = subprocess.run([sys.executable, "-c", READ_ONLY_RUN, config.POSTGRES_HOST, str(config.POSTGRES_PORT)],
                         capture_output=True, text=True, env=env, cwd=Path(__file__).resolve().parents[1],
                         timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result["passed"] == len(CHECKS), [c for c in result["checks"] if c["status"] != "passed"]
