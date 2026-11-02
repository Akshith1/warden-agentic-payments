"""The HTTP surface: submit, poll, list the approval queue, resume, and the
double-resume guard. Same real Postgres and ledger as test_graph; the LLM is
the same test double."""
import time

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from app import auth, main
from app.graph import build_graph

from .test_graph import FakeLLM, invoice


@pytest.fixture()
def api(ledger, monkeypatch):
    monkeypatch.setattr(main.config, "MLFLOW_TRACKING_URI", "")  # bundled prompt; the registry is tested separately
    fake = FakeLLM(invoice(vendor="Brand New LLC", total=75.0))
    monkeypatch.setattr(main, "build_graph", using(fake, ledger))
    with TestClient(main.app) as client:
        yield client


PEOPLE = {
    "clerk": auth.User("clerk", "Accounts Clerk", frozenset({"submitter"})),
    "amira": auth.User("amira", "Amira Approver", frozenset({"submitter", "approver"})),
    "rajesh": auth.User("rajesh", "Rajesh Vasa", frozenset({"submitter", "approver", "controller"})),
}


@pytest.fixture(autouse=True)
def signed_in():
    """Stands in for Keycloak (tested on its own in test_auth.py). The clerk submits and
    Amira decides, unless a request names someone else in X-Test-User."""
    def who(request: Request) -> auth.User:
        default = "amira" if request.url.path.endswith("/resume") else "clerk"
        return PEOPLE[request.headers.get("X-Test-User", default)]
    main.app.dependency_overrides[auth.current_user] = who
    yield
    main.app.dependency_overrides.pop(auth.current_user, None)


def using(fake, ledger):
    """Swaps the model and ledger in the service's own Deps, so the graph and the
    endpoints (which also call the ledger) share the test ledger."""
    def build(deps, cp):
        deps.llm, deps.ledger, deps.sleep = fake, ledger, (lambda s: None)
        return build_graph(deps, cp)
    return build


def wait_for(client, run_id, status, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = next(r for r in client.get("/runs").json() if r["run_id"] == run_id)
        if row["status"] == status:
            return row
        time.sleep(0.1)
    raise AssertionError(f"run {run_id} never reached {status}; last {row['status']}")


def test_submit_queue_resume(api):
    run_id = api.post("/runs", json={"invoice_text": "ACME invoice ..."}).json()["run_id"]
    row = wait_for(api, run_id, "pending_human")
    assert row["vendor_name"] == "Brand New LLC" and float(row["total_amount"]) == 75.0
    assert row["status_reason"] == "first-seen vendor"  # shown in the Runs list's Why column
    assert any(r["run_id"] == run_id for r in api.get("/runs", params={"status": "pending_human"}).json())

    detail = api.get(f"/runs/{run_id}").json()
    assert detail["next"] == ["human_review"] and detail["run"]["status"] == "pending_human"
    assert "first-seen vendor" in detail["awaiting_human"][0]["escalation_reasons"]

    assert api.post(f"/runs/{run_id}/resume", json={"decision": "approve"}).status_code == 202
    second = api.post(f"/runs/{run_id}/resume", json={"decision": "approve"})
    assert second.status_code == 409
    wait_for(api, run_id, "settled")


def test_unknown_run_and_validation(api):
    assert api.get("/runs/does-not-exist").status_code == 404
    assert api.post("/runs", json={"invoice_text": ""}).status_code == 422
    health = api.get("/health").json()
    assert health["extraction_prompt"] == {"source": "bundled", "version": None}
    assert health["rules"]["min_prior_payments"] == 2 and health["rules"]["ledger_currency"] == "MYR"


def test_failed_run_can_be_retried(ledger, monkeypatch):
    from .test_graph import seed_history
    seed_history(ledger, "Acme Supply", [100, 105])

    class OutOfCredit(FakeLLM):
        failures = 1

        def __call__(self, agent, route, system, user, model):
            if agent == "approval" and self.failures:
                self.failures -= 1
                raise RuntimeError("You have no credits remaining")
            return super().__call__(agent, route, system, user, model)

    fake = OutOfCredit(invoice(vendor="Acme Supply", total=55.0))
    monkeypatch.setattr(main.config, "MLFLOW_TRACKING_URI", "")
    monkeypatch.setattr(main, "build_graph", using(fake, ledger))
    with TestClient(main.app) as api:
        run_id = api.post("/runs", json={"invoice_text": "Acme ..."}).json()["run_id"]
        row = wait_for(api, run_id, "failed")
        assert "no credits remaining" in row["status_reason"]
        assert api.post(f"/runs/{run_id}/retry").status_code == 202
        assert api.post(f"/runs/{run_id}/retry").status_code == 409  # already claimed
        wait_for(api, run_id, "settled")


def test_inbox_lists_and_submits_dataset_receipts(api, ledger):
    from app.main import rt
    with rt.pool.connection() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS invoice_inbox (id BIGSERIAL PRIMARY KEY, source TEXT,
                        external_key TEXT UNIQUE, split TEXT, text TEXT, ground_truth JSONB)""")
        conn.execute("""INSERT INTO invoice_inbox (source, external_key, split, text, ground_truth)
                        VALUES ('sroie', 'XTEST1', 'test', 'BRAND NEW LLC TOTAL 75.00',
                                '{"entities": {"company": "BRAND NEW LLC", "total": "75.00"}}')
                        ON CONFLICT (external_key) DO NOTHING""")
    rows = api.get("/inbox").json()
    assert any(r["external_key"] == "XTEST1" and r["labelled_vendor"] == "BRAND NEW LLC" for r in rows)
    run_id = api.post("/inbox/XTEST1/submit").json()["run_id"]
    row = wait_for(api, run_id, "pending_human")
    assert row["source"] == "dataset"
    assert api.post("/inbox/NOPE/submit").status_code == 404


def test_approver_links_first_seen_vendor_to_an_existing_one(api, ledger):
    from .test_graph import seed_history
    seed_history(ledger, "Brand New Limited", [60, 70])
    vid = ledger.lookup_vendor("Brand New Limited")["vendor_id"]
    run_id = api.post("/runs", json={"invoice_text": "Brand New LLC ..."}).json()["run_id"]
    wait_for(api, run_id, "pending_human")

    body = {"decision": "approve"}
    assert api.post(f"/runs/{run_id}/resume", json=body | {"decision": "reject", "same_vendor_id": vid}).status_code == 422
    bad = api.post(f"/runs/{run_id}/resume", json=body | {"same_vendor_id": 999})
    assert bad.status_code == 409 and "no such vendor" in bad.json()["detail"]
    assert wait_for(api, run_id, "pending_human")  # still waiting; the person can choose again

    assert api.post(f"/runs/{run_id}/resume", json=body | {"same_vendor_id": vid}).status_code == 202
    wait_for(api, run_id, "settled")
    after = ledger.lookup_vendor("Brand New LLC")
    assert after["vendor_id"] == vid and after["payment_count"] == 3 and after["matched_alias"] == "Brand New LLC"


def test_a_known_vendor_cannot_be_relinked(ledger, monkeypatch):
    from .test_graph import seed_history
    seed_history(ledger, "Acme Supply", [100])  # known, but too little history: goes to a person
    seed_history(ledger, "Other Vendor", [100, 100])
    fake = FakeLLM(invoice(vendor="Acme Supply", total=40.0))
    monkeypatch.setattr(main.config, "MLFLOW_TRACKING_URI", "")
    monkeypatch.setattr(main, "build_graph", using(fake, ledger))
    with TestClient(main.app) as api:
        run_id = api.post("/runs", json={"invoice_text": "Acme ..."}).json()["run_id"]
        wait_for(api, run_id, "pending_human")
        other = ledger.lookup_vendor("Other Vendor")["vendor_id"]
        r = api.post(f"/runs/{run_id}/resume", json={"decision": "approve", "same_vendor_id": other})
        assert r.status_code == 409 and "only a first-seen vendor" in r.json()["detail"]


def test_policies_lists_cedar_rules_and_capability_manifests(api):
    body = api.get("/policies").json()
    ids = {r["id"] for r in body["cedar"]["rules"]}
    assert {"agents-vendor-rate-limit", "only-write-tier-writes-ledger"} <= ids
    writers = [m["agent"] for m in body["capabilities"] if "CreateLedgerEntry" in m["allowed_actions"]]
    assert writers == ["payment-execution"]


def test_image_submission_is_read_by_ocr_and_never_returned(ledger, monkeypatch):
    from .test_graph import FakeOcr
    fake = FakeLLM(invoice(vendor="Brand New LLC", total=75.0))
    ocr = FakeOcr("BRAND NEW LLC\nTotal 75.00")

    def build(deps, cp):
        deps.llm, deps.ledger, deps.sleep, deps.ocr = fake, ledger, (lambda s: None), ocr
        return build_graph(deps, cp)
    monkeypatch.setattr(main.config, "MLFLOW_TRACKING_URI", "")
    monkeypatch.setattr(main, "build_graph", build)
    with TestClient(main.app) as api:
        run_id = api.post("/runs/image", json={"image_base64": "iVBORw0KGgo=", "filename": "r.png"}).json()["run_id"]
        row = wait_for(api, run_id, "pending_human")
        assert row["source"] == "ocr" and ocr.calls == 1
        detail = api.get(f"/runs/{run_id}").json()
        assert "image_base64" not in detail["state"] and detail["state"]["invoice_text"].startswith("BRAND NEW")
        monkeypatch.setattr(main.config, "MAX_IMAGE_BYTES", 4)
        assert api.post("/runs/image", json={"image_base64": "A" * 100}).status_code == 413


def test_people_decide_by_role_and_never_their_own_invoice(api):
    run_id = api.post("/runs", json={"invoice_text": "Brand New LLC ..."},
                      headers={"X-Test-User": "amira"}).json()["run_id"]
    wait_for(api, run_id, "pending_human")
    clerk = api.post(f"/runs/{run_id}/resume", json={"decision": "approve"}, headers={"X-Test-User": "clerk"})
    assert clerk.status_code == 403 and clerk.json()["detail"] == "needs the approver or controller role"
    own = api.post(f"/runs/{run_id}/resume", json={"decision": "approve"})  # amira submitted it
    assert own.status_code == 403
    assert own.json()["detail"] == ("you can't approve this one: "
                                    "you submitted this invoice, so another person must approve it")
    assert wait_for(api, run_id, "pending_human")  # still waiting for someone who may
    ok = api.post(f"/runs/{run_id}/resume", json={"decision": "approve"}, headers={"X-Test-User": "rajesh"})
    assert ok.status_code == 202
    row = wait_for(api, run_id, "settled")
    assert row["submitted_by"] == "amira"
    trail = api.get(f"/runs/{run_id}").json()["state"]["audit_trail"]
    review = next(e for e in trail if e["step"] == "human_review")
    assert review["approver"] == "rajesh" and review["approver_name"] == "Rajesh Vasa"
    me = api.get("/me", headers={"X-Test-User": "clerk"}).json()
    assert me == {"username": "clerk", "name": "Accounts Clerk", "roles": ["submitter"]}


def test_stopped_run_fails_with_the_reason_then_retries_and_every_step_is_on_the_record(ledger, monkeypatch, switch):
    import sqlite3

    from .conftest import CONTROL_DB
    from .test_graph import seed_history
    seed_history(ledger, "Acme Supply", [100, 105])
    fake = FakeLLM(invoice(vendor="Acme Supply", total=65.0))

    def build(deps, cp):
        deps.llm, deps.ledger = fake, ledger
        deps.sleep = lambda s: switch.trip(reason="checking a supplier")  # tripped during the hold
        return build_graph(deps, cp)
    monkeypatch.setattr(main.config, "MLFLOW_TRACKING_URI", "")
    monkeypatch.setattr(main, "build_graph", build)
    with TestClient(main.app) as api:
        run_id = api.post("/runs", json={"invoice_text": "Acme ..."}).json()["run_id"]
        row = wait_for(api, run_id, "failed")
        assert row["status_reason"] == "kill switch: kill switch is on: checking a supplier (by tester)"
        assert api.post(f"/runs/{run_id}/retry").status_code == 202
        wait_for(api, run_id, "failed")  # still on: stops again at the same place
        switch.reset()
        rt = main.rt
        rt.deps.sleep = lambda s: None
        assert api.post(f"/runs/{run_id}/retry").status_code == 202
        wait_for(api, run_id, "settled")

    db = sqlite3.connect(CONTROL_DB)
    events = db.execute("SELECT type, actor, json_extract(payload, '$.step') FROM audit WHERE run_id = ? "
                        "ORDER BY seq", (run_id,)).fetchall()
    steps = [e[2] for e in events if e[0] == "run_step"]
    assert events[0][:2] == ("run_submitted", "clerk")
    assert steps == ["guardrail", "extraction", "risk_scoring", "approval", "critic", "policy", "dry_run",
                     "settlement_hold", "commit"]  # each once, in order, despite two stops and retries
    assert [e[0] for e in events].count("run_stopped") == 2
