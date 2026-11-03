"""The SPIRE bootstrap job against a stand-in spire-server CLI."""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BOOTSTRAP = HERE.parent / "bootstrap" / "bootstrap.py"
IDENTITIES = HERE.parent / "identities.json"


def bootstrap(tmp_path, identities=IDENTITIES, healthy_after=0):
    state = tmp_path / "state.json"
    if not state.exists():
        state.write_text(json.dumps({"entries": [], "healthy_after": healthy_after, "calls": 0}))
    env = {"PATH": os.environ["PATH"], "FAKE_SPIRE_STATE": str(state), "SPIRE_SERVER_BIN": str(HERE / "fake_spire_server.py"),
           "SPIRE_BOOTSTRAP_DIR": str(tmp_path / "out"), "SPIRE_IDENTITIES": str(identities)}
    out = subprocess.run([sys.executable, str(BOOTSTRAP)], env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout, json.loads(state.read_text())["entries"]


def test_first_start_writes_the_bundle_a_token_and_every_identity(tmp_path):
    out, entries = bootstrap(tmp_path, healthy_after=2)   # the server takes a moment to come up
    assert (tmp_path / "out" / "bundle.pem").read_text().startswith("-----BEGIN CERTIFICATE-----")
    assert len((tmp_path / "out" / "join_token").read_text()) == 36
    assert {e["id"] for e in entries} == {"warden-backend", "warden-policy", "warden-ledger"}
    backend = next(e for e in entries if e["id"] == "warden-backend")
    assert backend["spiffe_id"] == {"trust_domain": "warden.local", "path": "/backend"}
    # SPIRE splits a selector at the first colon: type "docker", value "label:<key>:<value>".
    assert {(s["type"], s["value"]) for s in backend["selectors"]} == {
        ("docker", "label:com.docker.compose.project:warden"),
        ("docker", "label:com.docker.compose.service:warden-backend")}
    assert "created spiffe://warden.local/backend" in out


def test_a_second_start_changes_nothing_but_the_token(tmp_path):
    bootstrap(tmp_path)
    first_token = (tmp_path / "out" / "join_token").read_text()
    out, _ = bootstrap(tmp_path)
    assert "identities already match identities.json" in out
    assert (tmp_path / "out" / "join_token").read_text() != first_token


def test_the_file_is_the_truth(tmp_path):
    bootstrap(tmp_path)
    config = json.loads(IDENTITIES.read_text())
    config["identities"] = [i for i in config["identities"] if i["entry_id"] != "warden-policy"]
    config["identities"][0]["selectors"] = config["identities"][0]["selectors"][:1]
    changed = tmp_path / "identities.json"
    changed.write_text(json.dumps(config))
    out, entries = bootstrap(tmp_path, identities=changed)
    assert "deleted spiffe://warden.local/policy" in out and "updated spiffe://warden.local/backend" in out
    assert {e["id"] for e in entries} == {"warden-backend", "warden-ledger"}
