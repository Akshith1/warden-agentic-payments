"""Runs against a real Postgres (set PGPASS_WARDEN_LEDGER, POSTGRES_HOST,
POSTGRES_PORT, LEDGER_DB). Each test gets a freshly truncated ledger."""
import os
import uuid

import pytest

os.environ.setdefault("LEDGER_WRITE_KEY", "test-write-key")
os.environ.setdefault("VENDOR_LOOKUP_KEY", "test-lookup-key")
os.environ.setdefault("OPENING_CASH", "1000.00")

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402

W = {"X-API-Key": "test-write-key"}
R = {"X-API-Key": "test-lookup-key"}


@pytest.fixture()
def client():
    with db.connect() as conn:
        db.init_schema(conn)
        conn.execute("TRUNCATE postings, transactions, historical_payments, vendors RESTART IDENTITY CASCADE")
        conn.execute("UPDATE accounts SET balance = 0")
    with TestClient(app) as c:  # lifespan posts the opening balance
        yield c


def pay(amount, key=None, cash="1000-operating-cash", expense="5000-vendor-expense"):
    return {
        "idempotency_key": key or str(uuid.uuid4()),
        "description": "test payment",
        "postings": [
            {"account_id": expense, "direction": "debit", "amount": str(amount)},
            {"account_id": cash, "direction": "credit", "amount": str(amount)},
        ],
    }


def balances(client):
    return {a["id"]: float(a["balance"]) for a in client.get("/accounts", headers=R).json()}


def test_opening_balance_posted_once(client):
    assert balances(client)["1000-operating-cash"] == 1000.0
    with TestClient(app):
        pass  # a second startup must not post it again
    assert balances(client)["1000-operating-cash"] == 1000.0


def test_commit_moves_balances(client):
    r = client.post("/transactions", json=pay("250.00"), headers=W)
    assert r.status_code == 200 and r.json()["valid"]
    b = balances(client)
    assert b["1000-operating-cash"] == 750.0 and b["5000-vendor-expense"] == 250.0


def test_dry_run_reports_and_writes_nothing(client):
    r = client.post("/transactions/dry-run", json=pay("250.00"), headers=W).json()
    assert r["valid"] and float(r["resulting_balances"]["1000-operating-cash"]) == 750.0
    assert balances(client)["1000-operating-cash"] == 1000.0
    assert len(client.get("/transactions", headers=R).json()) == 1  # only the opening entry


def test_insufficient_funds(client):
    r = client.post("/transactions/dry-run", json=pay("1000.01"), headers=W).json()
    assert not r["valid"] and any("insufficient funds" in e for e in r["errors"])
    assert client.post("/transactions", json=pay("1000.01"), headers=W).status_code == 422


def test_unbalanced_entry(client):
    body = pay("10.00")
    body["postings"][1]["amount"] = "9.99"
    r = client.post("/transactions/dry-run", json=body, headers=W).json()
    assert not r["valid"] and any("unbalanced" in e for e in r["errors"])


def test_unknown_account(client):
    r = client.post("/transactions/dry-run", json=pay("10.00", expense="9999-nope"), headers=W).json()
    assert not r["valid"] and "unknown account: 9999-nope" in r["errors"]


def test_duplicate_idempotency_key(client):
    assert client.post("/transactions", json=pay("10.00", key="inv-1"), headers=W).status_code == 200
    assert client.post("/transactions", json=pay("10.00", key="inv-1"), headers=W).status_code == 409
    assert balances(client)["1000-operating-cash"] == 990.0


def test_keys_are_separate(client):
    assert client.post("/transactions", json=pay("10.00"), headers=R).status_code == 401
    assert client.get("/vendors/lookup", params={"name": "x"}, headers=W).status_code == 401


def test_vendor_history_builds_trust(client):
    before = client.get("/vendors/lookup", params={"name": "Acme Supply, Inc."}, headers=R).json()
    assert before["known"] is False
    for i, amt in enumerate(["100.00", "120.00", "110.00"]):
        r = client.post("/vendors/record-payment", headers=W, json={
            "vendor_name": "Acme Supply, Inc.", "amount": amt, "paid_on": f"2026-01-0{i + 1}",
            "external_ref": f"ref-{i}", "remit_account": "ACCT-1"})
        assert r.json()["recorded"]
    again = client.post("/vendors/record-payment", headers=W, json={
        "vendor_name": "ACME SUPPLY INC", "amount": "100.00", "paid_on": "2026-01-01", "external_ref": "ref-0"})
    assert again.json()["recorded"] is False
    after = client.get("/vendors/lookup", params={"name": "acme supply inc"}, headers=R).json()
    assert after["known"] and after["payment_count"] == 3
    assert float(after["average_amount"]) == 110.0
    assert after["first_seen"] == "2026-01-01" and after["last_remit_account"] == "ACCT-1"
