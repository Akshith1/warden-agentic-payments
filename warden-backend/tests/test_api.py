"""The HTTP surface: submit, poll, list the approval queue, resume, and the
double-resume guard. Same real Postgres and ledger as test_graph; the LLM is
the same test double."""
import time

import pytest
from fastapi.testclient import TestClient

from app import main
from app.agents import Deps
from app.graph import build_graph

from .test_graph import FakeLLM, invoice


@pytest.fixture()
def api(ledger, monkeypatch):
    monkeypatch.setattr(main.config, "MLFLOW_TRACKING_URI", "")  # bundled prompt; the registry is tested separately
    fake = FakeLLM(invoice(vendor="Brand New LLC", total=75.0))
    monkeypatch.setattr(main, "build_graph", lambda deps, cp: build_graph(
        Deps(llm=fake, ledger=ledger, sleep=lambda s: None), cp))
    with TestClient(main.app) as client:
        yield client


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
    assert detail["next"] == ["human_review"]
    assert "first-seen vendor" in detail["awaiting_human"][0]["escalation_reasons"]

    assert api.post(f"/runs/{run_id}/resume", json={"decision": "approve", "approver": "c1"}).status_code == 202
    second = api.post(f"/runs/{run_id}/resume", json={"decision": "approve", "approver": "c1"})
    assert second.status_code == 409
    wait_for(api, run_id, "settled")


def test_unknown_run_and_validation(api):
    assert api.get("/runs/does-not-exist").status_code == 404
    assert api.post("/runs", json={"invoice_text": ""}).status_code == 422
    assert api.get("/health").json() == {"status": "ok",
                                         "extraction_prompt": {"source": "bundled", "version": None}}


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
    monkeypatch.setattr(main, "build_graph", lambda deps, cp: build_graph(
        Deps(llm=fake, ledger=ledger, sleep=lambda s: None), cp))
    with TestClient(main.app) as api:
        run_id = api.post("/runs", json={"invoice_text": "Acme ..."}).json()["run_id"]
        row = wait_for(api, run_id, "failed")
        assert "no credits remaining" in row["status_reason"]
        assert api.post(f"/runs/{run_id}/retry").status_code == 202
        assert api.post(f"/runs/{run_id}/retry").status_code == 409  # already claimed
        wait_for(api, run_id, "settled")
