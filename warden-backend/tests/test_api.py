"""The HTTP surface: submit, poll, list the approval queue, resume, and the
double-resume guard. Same real Postgres and ledger as test_graph; the LLM is
the same test double."""
import time

import pytest
from fastapi.testclient import TestClient

from app import main
from app.graph import build_graph

from .test_graph import FakeLLM, invoice


@pytest.fixture()
def api(ledger, monkeypatch):
    monkeypatch.setattr(main.config, "MLFLOW_TRACKING_URI", "")  # bundled prompt; the registry is tested separately
    fake = FakeLLM(invoice(vendor="Brand New LLC", total=75.0))
    monkeypatch.setattr(main, "build_graph", using(fake, ledger))
    with TestClient(main.app) as client:
        yield client


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
    assert any(r["run_id"] == run_id for r in api.get("/runs", params={"status": "pending_human"}).json())

    detail = api.get(f"/runs/{run_id}").json()
    assert detail["next"] == ["human_review"] and detail["run"]["status"] == "pending_human"
    assert "first-seen vendor" in detail["awaiting_human"][0]["escalation_reasons"]

    assert api.post(f"/runs/{run_id}/resume", json={"decision": "approve", "approver": "c1"}).status_code == 202
    second = api.post(f"/runs/{run_id}/resume", json={"decision": "approve", "approver": "c1"})
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

    body = {"decision": "approve", "approver": "c1"}
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
        r = api.post(f"/runs/{run_id}/resume", json={"decision": "approve", "approver": "c1", "same_vendor_id": other})
        assert r.status_code == 409 and "only a first-seen vendor" in r.json()["detail"]
