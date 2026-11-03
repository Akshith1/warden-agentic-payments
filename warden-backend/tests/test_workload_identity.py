"""Phase 6b end to end: a real graph run pays an invoice through a real ledger
process running with LEDGER_AUTH=spiffe, using JWT-SVIDs from a stand-in SPIRE
agent that speaks the real Workload API. No ledger key is involved."""
import importlib.util
import os
import sys
import time
from pathlib import Path

import pytest
from spiffe import WorkloadApiClient

from app import workload_identity
from app.ledger_client import LedgerClient

from .conftest import LEDGER_DIR, serve
from .test_graph import FakeLLM, cash, invoice, run, seed_history

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("fake_workload_api", ROOT / "infra/spire/fake_workload_api.py")
fake = importlib.util.module_from_spec(_spec)
sys.modules["fake_workload_api"] = fake
_spec.loader.exec_module(fake)
SPIFFE_LEDGER_PORT = 18005


@pytest.fixture(scope="module")
def agent():
    folder = Path(f"/tmp/spire-test-{os.getpid()}-{time.monotonic_ns()}")
    folder.mkdir()
    with fake.FakeAgent(folder) as a:
        yield a


@pytest.fixture(scope="module")
def spiffe_ledger_url(agent):
    proc, url = serve(LEDGER_DIR, SPIFFE_LEDGER_PORT, {
        "LEDGER_AUTH": "spiffe", "SPIFFE_ENDPOINT_SOCKET": agent.socket_for("spiffe://warden.local/ledger"),
        "OTEL_SDK_DISABLED": "true"})
    yield url
    proc.terminate()


def client_as(agent, url, spiffe_id, monkeypatch) -> LedgerClient:
    monkeypatch.setattr(workload_identity, "MODE", "spiffe")
    client = LedgerClient(base_url=url)
    client._identity = workload_identity.Identity(
        client=WorkloadApiClient(socket_path=agent.socket_for(spiffe_id)))
    return client


def test_a_payment_settles_with_workload_identity_and_no_key(checkpointer, ledger, ledger_url, spiffe_ledger_url,
                                                               agent, monkeypatch):
    seed_history(ledger, "Identity Supply", [100, 120, 110])
    as_backend = client_as(agent, spiffe_ledger_url, "spiffe://warden.local/backend", monkeypatch)
    monkeypatch.setattr("app.config.LEDGER_WRITE_KEY", "")     # the backend holds no write key at all
    graph, cfg = run(checkpointer, as_backend, FakeLLM(invoice(vendor="Identity Supply", total=75.0)))
    assert graph.get_state(cfg).values["status"] == "settled"
    assert cash(ledger_url) == 10000 - 75
    # One token per run: fetched once, reused for every ledger call, renewed only near expiry.
    assert len(agent.issued) == 1


def test_another_workload_cannot_pay(checkpointer, ledger, spiffe_ledger_url, agent, monkeypatch):
    seed_history(ledger, "Identity Supply", [100, 120, 110])
    as_policy = client_as(agent, spiffe_ledger_url, "spiffe://warden.local/policy", monkeypatch)
    assert as_policy.lookup_vendor("Identity Supply")["known"] is True       # reading is allowed
    with pytest.raises(Exception) as refused:
        as_policy.dry_run({"idempotency_key": "x", "description": "x", "postings": []})
    assert refused.value.response.status_code == 403


def test_a_token_is_renewed_before_it_expires(agent):
    client = WorkloadApiClient(socket_path=agent.socket_for("spiffe://warden.local/backend"))
    identity = workload_identity.Identity(client=client)
    first = identity.token()
    assert identity.token() == first                      # still valid: reused
    identity._expiry = time.time() + 30                   # within a minute of expiry
    assert identity.token() != first                      # renewed
