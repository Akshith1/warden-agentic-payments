"""Phase 6b: the ledger with LEDGER_AUTH=spiffe, against a stand-in SPIRE agent
that speaks the real Workload API and signs real JWT-SVIDs."""
import importlib.util
import sys
import time
from pathlib import Path

import pytest
from spiffe import WorkloadApiClient

from app import auth

from .test_ledger import R, W, client, pay  # noqa: F401  (client is a fixture)

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("fake_workload_api", ROOT / "infra/spire/fake_workload_api.py")
fake = importlib.util.module_from_spec(_spec)
sys.modules["fake_workload_api"] = fake
_spec.loader.exec_module(fake)

BACKEND, POLICY, STRANGER = ("spiffe://warden.local/backend", "spiffe://warden.local/policy",
                             "spiffe://warden.local/console")


@pytest.fixture()
def agent(tmp_path, monkeypatch):
    short = Path(f"/tmp/spire-test-{time.monotonic_ns()}")   # Unix socket paths must stay short
    short.mkdir()
    with fake.FakeAgent(short) as a:
        monkeypatch.setattr(auth, "MODE", "spiffe")
        monkeypatch.setattr(auth, "_validated", auth._Validated())
        monkeypatch.setattr(auth, "_client", WorkloadApiClient(socket_path=a.socket_for("spiffe://warden.local/ledger")))
        yield a


def bearer(agent, spiffe_id, audience="warden-ledger", ttl=None):
    return {"Authorization": f"Bearer {agent.issue(spiffe_id, [audience], ttl)}"}


def token_from_workload_api(agent, spiffe_id):
    """What the backend does: ask its own agent socket for a token for the ledger."""
    svid = WorkloadApiClient(socket_path=agent.socket_for(spiffe_id)).fetch_jwt_svid(audience={"warden-ledger"})
    return {"Authorization": f"Bearer {svid.token}"}


def test_the_backend_writes_with_its_identity_and_no_key(client, agent):
    r = client.post("/transactions/dry-run", json=pay(10), headers=token_from_workload_api(agent, BACKEND))
    assert r.status_code == 200 and r.json()["valid"] is True


def test_the_write_key_stops_working(client, agent):
    r = client.post("/transactions", json=pay(10), headers=W)
    assert r.status_code == 401 and r.json()["detail"] == "the ledger takes writes from workload identities only"


def test_the_policy_service_may_read_but_not_write(client, agent):
    headers = token_from_workload_api(agent, POLICY)
    assert client.get("/vendors/lookup", params={"name": "Acme"}, headers=headers).status_code == 200
    r = client.post("/transactions", json=pay(10), headers=headers)
    assert r.status_code == 403 and r.json()["detail"] == f"{POLICY} may not write the ledger"


def test_an_identity_with_no_ledger_rights_is_refused(client, agent):
    r = client.get("/vendors/lookup", params={"name": "Acme"}, headers=bearer(agent, STRANGER))
    assert r.status_code == 403


def test_the_console_still_reads_with_the_read_key(client, agent):
    assert client.get("/accounts", headers=R).status_code == 200


@pytest.mark.parametrize("make", [
    pytest.param(lambda a: bearer(a, BACKEND, audience="warden-control"), id="meant for another service"),
    pytest.param(lambda a: bearer(a, BACKEND, ttl=-60), id="expired"),
    pytest.param(lambda a: {"Authorization": "Bearer not-a-token"}, id="not a token"),
])
def test_bad_tokens_are_refused(client, agent, make):
    r = client.post("/transactions", json=pay(10), headers=make(agent))
    assert r.status_code == 401 and r.json()["detail"].startswith("workload identity not accepted")


def test_a_token_signed_by_another_trust_domain_is_refused(client, agent, tmp_path):
    with fake.FakeAgent(Path(f"/tmp/spire-test-{time.monotonic_ns()}")) as other:
        other.folder.mkdir()
        r = client.post("/transactions", json=pay(10), headers=bearer(other, BACKEND))
    assert r.status_code == 401


def test_each_token_is_checked_with_the_agent_once(client, agent, monkeypatch):
    calls = []
    real = auth._client.validate_jwt_svid
    monkeypatch.setattr(auth._client, "validate_jwt_svid", lambda *a, **k: calls.append(1) or real(*a, **k))
    headers = bearer(agent, BACKEND)
    for _ in range(3):
        assert client.get("/accounts", headers=headers).status_code == 200
    assert len(calls) == 1


def test_keys_mode_is_unchanged(client):
    assert auth.MODE == "keys"
    assert client.post("/transactions/dry-run", json=pay(10), headers=W).status_code == 200
    assert client.post("/transactions/dry-run", json=pay(10),
                       headers={"Authorization": "Bearer anything"}).status_code == 401
