"""The HTTP surface. The ledger lookup is replaced; everything else is real."""
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ["POLICY_DIR"] = str(Path(__file__).resolve().parents[2] / "policies")

from app import main  # noqa: E402

HISTORY = {"known": True, "vendor_id": 7, "vendor_name": "ACME SUPPLY", "payment_count": 3, "payments_last_24h": 0}


@pytest.fixture()
def api(monkeypatch):
    history = dict(HISTORY)
    main.state.allowed.clear()
    monkeypatch.setattr(main, "vendor_history", lambda name: history)
    with TestClient(main.app) as client:
        client.history = history
        yield client


def approve(client, who=("Agent", "approval"), roles=(), **payment):
    body = {"principal": {"type": who[0], "id": who[1], "roles": list(roles)}, "action": "ApprovePayment",
            "payment": {"amount": "120.00", "currency": "MYR", "vendor": "Acme Supply", "confidence": 0.95,
                        "critic_agrees": True} | payment}
    return client.post("/authorize", json=body).json()


def test_a_burst_is_limited_before_anything_settles(api):
    # Four invoices from one vendor approved back to back: the ledger still shows no recent
    # payment, but the service remembers what it allowed. People are not counted or limited.
    assert [approve(api)["decision"] for _ in range(3)] == ["Allow"] * 3
    fourth = approve(api)
    assert fourth["decision"] == "Deny" and fourth["policies"] == ["agents-vendor-rate-limit"]
    assert fourth["facts"]["paymentsLast24h"] == 3
    assert approve(api, who=("User", "rajesh"), roles=["approver"])["decision"] == "Allow"
    api.history["vendor_id"] = 8  # another vendor has its own count
    assert approve(api)["decision"] == "Allow"


def test_routine_payment_is_allowed_with_facts_from_the_ledger(api):
    r = approve(api)
    assert r["decision"] == "Allow" and r["policies"] == ["approval-agent-may-approve"]
    assert r["facts"]["priorPayments"] == 3 and r["facts"]["vendor"] == "ACME SUPPLY"


def test_every_failing_rule_is_reported_with_its_reason(api):
    api.history.update(known=False, payment_count=0)
    r = approve(api, amount="6000.00", currency="usd")
    assert r["decision"] == "Deny"
    assert set(r["policies"]) >= {"agents-routine-amounts-only", "agents-need-vendor-history",
                                  "agents-ledger-currency-only"}
    assert "above the 5,000.00 routine amount, so a person must approve" in r["reasons"]


def test_fourth_payment_in_a_day_needs_a_person(api):
    api.history["payments_last_24h"] = 3
    agent = approve(api)
    assert agent["decision"] == "Deny" and agent["policies"] == ["agents-vendor-rate-limit"]
    assert approve(api, who=("User", "rajesh"), roles=["approver"])["decision"] == "Allow"


def test_only_payment_execution_may_write_the_ledger(api):
    for agent, expected in [("payment-execution", "Allow"), ("extraction", "Deny"), ("risk-scoring", "Deny"),
                            ("approval", "Deny"), ("critic", "Deny")]:
        r = api.post("/authorize", json={"principal": {"type": "Agent", "id": agent}, "action": "CreateLedgerEntry",
                                         "payment": {"amount": "10", "currency": "MYR", "vendor": "Acme"}}).json()
        assert r["decision"] == expected, agent
    denied = r["reasons"]
    assert denied == ["only the payment-execution agent may write to the ledger"]


def test_a_claimed_trust_tier_is_ignored(api):
    # Trust tiers come from the service's registry; extra fields from the caller are not used.
    r = api.post("/authorize", json={"principal": {"type": "Agent", "id": "extraction", "trustTier": "write-ledger"},
                                     "action": "CreateLedgerEntry",
                                     "payment": {"amount": "10", "currency": "MYR", "vendor": "Acme"}}).json()
    assert r["decision"] == "Deny"


def test_vendor_actions_and_policy_listing(api):
    r = api.post("/authorize", json={"principal": {"type": "Agent", "id": "extraction"}, "action": "LookupVendor",
                                     "vendor": "Acme"}).json()
    assert r["decision"] == "Deny" and r["reasons"] == ["no policy permits extraction to LookupVendor"]
    listing = api.get("/policies").json()
    assert len(listing["rules"]) == 16 and listing["digest"] == api.get("/health").json()["policy_set"]
    assert api.post("/authorize", json={"principal": {"type": "User", "id": "x"},
                                        "action": "ApprovePayment"}).status_code == 422


def test_no_ledger_no_decision(api, monkeypatch):
    import httpx

    def down(name):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(main, "vendor_history", down)
    r = api.post("/authorize", json={"principal": {"type": "Agent", "id": "approval"}, "action": "ApprovePayment",
                                     "payment": {"amount": "10", "currency": "MYR", "vendor": "Acme"}})
    assert r.status_code == 503


def test_people_approve_by_role_and_never_their_own_invoice(api):
    person = ("User", "amira")
    assert approve(api, who=person, roles=["approver"], amount="49999.99")["decision"] == "Allow"
    over = approve(api, who=person, roles=["approver"], amount="50000.01")
    assert over["decision"] == "Deny" and over["reasons"] == ["no policy permits amira to ApprovePayment"]
    assert approve(api, who=person, roles=["approver", "controller"], amount="50000.01")["decision"] == "Allow"
    assert approve(api, who=person, roles=["submitter"])["decision"] == "Deny"
    own = approve(api, who=person, roles=["approver", "controller"], submitted_by="amira")
    assert own["decision"] == "Deny" and own["policies"] == ["four-eyes"]
    assert own["reasons"] == ["you submitted this invoice, so another person must approve it"]
