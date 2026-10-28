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
    assert api.get("/health").json() == {"status": "ok"}
