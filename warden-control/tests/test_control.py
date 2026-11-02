"""The control plane with a real SQLite file and real signed tokens; only
Keycloak's key download is replaced."""
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app import auth, config, main

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class Keys:
    def get_signing_key_from_jwt(self, token):
        return type("K", (), {"key": KEY.public_key()})()


def headers(username, *roles):
    now = int(time.time())
    token = jwt.encode({"iss": config.KEYCLOAK_ISSUER, "aud": "warden-backend", "sub": username, "exp": now + 300,
                        "preferred_username": username, "realm_access": {"roles": list(roles)}}, KEY,
                       algorithm="RS256")
    return {"Authorization": f"Bearer {token}"}


CLERK, AMIRA, RAJESH = headers("clerk", "submitter"), headers("amira", "approver"), headers("rajesh", "controller")


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "_jwks", Keys())
    monkeypatch.setattr(config, "CONTROL_DB", str(tmp_path / "control.db"))
    with TestClient(main.app) as client:
        yield client


def step(api, run_id, **payload):
    return api.post("/events", json={"type": "run_step", "run_id": run_id, "payload": payload})


def test_the_chain_verifies_and_catches_an_edit(api):
    for i in range(4):
        step(api, f"r{i}", step="human_review", decision="approve", approver="amira")
    assert api.get("/audit/verify", headers=CLERK).json() | {"head": None} == {"valid": True, "entries": 4,
                                                                              "head": None}
    db = sqlite3.connect(config.CONTROL_DB, isolation_level=None)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("UPDATE audit SET payload = '{}' WHERE seq = 2")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("DELETE FROM audit WHERE seq = 2")

    # Scenario 15: someone with the file drops the trigger and rewrites history.
    out = subprocess.run([sys.executable, "-m", "app.tamper", "--seq", "2"], capture_output=True, text=True,
                         cwd=Path(__file__).resolve().parents[1], env={"CONTROL_DB": config.CONTROL_DB})
    assert "rewrote entry 2" in out.stdout, out.stderr
    assert '"decision": "reject"' in db.execute("SELECT payload FROM audit WHERE seq = 2").fetchone()[0]
    result = api.get("/audit/verify", headers=CLERK).json()
    assert result == {"valid": False, "entries": 1, "broken_at": 2,
                      "problem": "entry 2 was changed after it was written"}


def test_a_deleted_entry_is_caught(api):
    for i in range(3):
        step(api, "r", step="extraction", n=i)
    db = sqlite3.connect(config.CONTROL_DB, isolation_level=None)
    db.execute("DROP TRIGGER audit_no_delete")
    db.execute("DELETE FROM audit WHERE seq = 2")
    assert api.get("/audit/verify", headers=CLERK).json()["problem"] == "entry 2 is missing"


def test_people_trip_and_only_controllers_reset(api):
    assert api.get("/status").json()["global_stop"] is False
    assert api.post("/kill-switch/trip", json={"reason": "suspicious batch"}, headers=CLERK).status_code == 403
    stopped = api.post("/kill-switch/trip", json={"reason": "suspicious batch"}, headers=AMIRA).json()
    assert stopped["global_stop"] and stopped["global"]["by"] == "amira"
    assert api.post("/kill-switch/reset", json={}, headers=AMIRA).status_code == 403
    assert api.post("/kill-switch/reset", json={}, headers=RAJESH).json()["global_stop"] is False
    assert api.post("/kill-switch/reset", json={}, headers=RAJESH).status_code == 409
    types = [e["type"] for e in api.get("/audit", headers=CLERK).json()]
    assert types == ["kill_switch_reset", "kill_switch_tripped"]  # newest first, both on the record
    assert api.post("/kill-switch/trip", json={"reason": "x"}).status_code == 401


def test_a_canary_vendor_stops_everything(api):
    # Scenario 14, trigger A.
    step(api, "r1", step="extraction", vendor="Acme Supply")
    assert api.get("/status").json()["global_stop"] is False
    step(api, "r2", step="extraction", vendor="Canary-Do-Not-Pay Sdn Bhd")
    status = api.get("/status").json()
    assert status["global_stop"] and status["global"]["by"] == "watchdog"
    assert "CANARY-DO-NOT-PAY" in status["global"]["reason"] and "r2" in status["global"]["reason"]


def test_an_agent_refused_again_and_again_is_paused(api, monkeypatch):
    # Scenario 14, trigger B: five policy refusals for one agent within ten minutes.
    for i in range(4):
        step(api, f"r{i}", step="policy", decision="Deny", principal="Agent:approval")
    step(api, "p", step="policy", decision="Deny", principal="User:clerk")  # people don't count
    assert api.get("/status").json()["paused_agents"] == []
    step(api, "r5", step="policy", decision="Deny", principal="Agent:approval")
    assert api.get("/status").json()["paused_agents"] == ["approval"]
    reset = api.post("/kill-switch/reset", json={"scope": "agent", "target": "approval"}, headers=RAJESH)
    assert reset.json()["paused_agents"] == []


def test_reading_the_log_needs_a_sign_in(api):
    assert api.get("/audit").status_code == 401
    assert api.get("/audit/verify").status_code == 401
