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


def record(client, name, ref, amount="100.00", remit="ACCT-1"):
    return client.post("/vendors/record-payment", headers=W, json={
        "vendor_name": name, "amount": amount, "paid_on": "2026-01-01", "external_ref": ref,
        "remit_account": remit}).json()


def test_confirmed_alias_shares_the_vendors_history(client):
    # The phase 3 live run: the same shop read as two different names.
    vid = record(client, "TAN CHAY YEE *** COPY *** OJC MARKETING SDN BHD", "r1")["vendor_id"]
    new_name = "TAN CHAY YEE OJC MARKETING SDN BHD"
    assert client.get("/vendors/lookup", params={"name": new_name}, headers=R).json()["known"] is False

    r = client.post("/vendors/aliases", headers=W,
                    json={"alias": new_name, "vendor_id": vid, "confirmed_by": "Rajesh V", "run_ref": "run-1"})
    assert r.status_code == 201 and r.json()["created"]
    found = client.get("/vendors/lookup", params={"name": new_name}, headers=R).json()
    assert found["known"] and found["vendor_id"] == vid and found["payment_count"] == 1
    assert found["matched_alias"] == new_name
    assert found["vendor_name"] == "TAN CHAY YEE *** COPY *** OJC MARKETING SDN BHD"

    # A payment under the alias lands in the same history, not a new vendor.
    assert record(client, new_name, "r2")["vendor_id"] == vid
    assert client.get("/vendors/lookup", params={"name": new_name}, headers=R).json()["payment_count"] == 2


def test_alias_confirmation_is_idempotent_and_conflicts_are_refused(client):
    a = record(client, "OJC MARKETING SDN BHD", "r1")["vendor_id"]
    b = record(client, "BRIGHTLINE OFFICE SUPPLIES", "r2")["vendor_id"]
    body = {"alias": "OJC MKTG", "vendor_id": a, "confirmed_by": "p1"}
    assert client.post("/vendors/aliases", headers=W, json=body).status_code == 201
    again = client.post("/vendors/aliases", headers=W, json=body)
    assert again.status_code == 201 and again.json()["created"] is False
    # The same alias can't be pointed at a second vendor, nor can a vendor's own name.
    assert client.post("/vendors/aliases", headers=W, json=body | {"vendor_id": b}).status_code == 409
    own = {"alias": "Brightline Office Supplies", "vendor_id": a, "confirmed_by": "p1"}
    assert client.post("/vendors/aliases", headers=W, json=own).status_code == 409
    assert client.post("/vendors/aliases", headers=W, json=body | {"vendor_id": 999}).status_code == 404
    assert client.post("/vendors/aliases", headers=W, json=body | {"alias": "***"}).status_code == 422


def test_alias_needs_the_write_key(client):
    vid = record(client, "OJC MARKETING SDN BHD", "r1")["vendor_id"]
    body = {"alias": "OJC", "vendor_id": vid, "confirmed_by": "p1"}
    assert client.post("/vendors/aliases", headers=R, json=body).status_code == 401


def test_search_lists_candidates_but_links_nothing(client):
    record(client, "TAN CHAY YEE *** COPY *** OJC MARKETING SDN BHD", "r1", remit="ACCT-9")
    record(client, "OJC MARKETING SDN BHD", "r2")
    record(client, "BRIGHTLINE OFFICE SUPPLIES SDN BHD", "r3")  # shares only "SDN BHD"
    hits = client.get("/vendors/search", params={"q": "TAN CHAY YEE OJC MARKETING SDN BHD"}, headers=R).json()
    assert [h["vendor_name"] for h in hits][:2] == ["TAN CHAY YEE *** COPY *** OJC MARKETING SDN BHD",
                                                     "OJC MARKETING SDN BHD"]
    assert hits[0]["payment_count"] == 1 and hits[0]["last_remit_account"] == "ACCT-9"
    assert all("BRIGHTLINE" not in h["vendor_name"] for h in hits)
    assert client.get("/vendors/search", params={"q": "**"}, headers=R).json() == []
    lookup = client.get("/vendors/lookup", params={"name": "TAN CHAY YEE OJC MARKETING SDN BHD"}, headers=R)
    assert lookup.json()["known"] is False  # searching created no link
