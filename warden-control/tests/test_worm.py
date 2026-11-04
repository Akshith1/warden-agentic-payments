"""Phase 11b: the write-once copy of the audit log. Real Store (SQLite), real Blob REST
calls, against a stand-in container that behaves as one with a time-based immutability
policy and protected append writes: blobs can be created and appended to, never
overwritten or deleted."""
import json
import sqlite3
from datetime import datetime, timezone
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from app import auth, config, main, worm
from app.store import GENESIS, Store, entry_hash

from .test_control import postgres_db  # noqa: F401  (a fixture)

URL = "https://wardenaudit.blob.core.windows.net/audit"


class ImmutableContainer:
    def __init__(self):
        self.blobs: dict[str, bytearray] = {}
        self.calls: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer storage-token"
        assert request.headers["x-ms-version"] == worm.API_VERSION
        query = parse_qs(request.url.query.decode())
        name = request.url.path.removeprefix("/audit").lstrip("/")
        self.calls.append(f"{request.method} {name or '(list)'} {query.get('comp', [''])[0]}".strip())
        if request.method == "GET" and not name:
            prefix = query.get("prefix", [""])[0]
            names = "".join(f"<Blob><Name>{n}</Name></Blob>" for n in sorted(self.blobs) if n.startswith(prefix))
            return httpx.Response(200, content=f"<EnumerationResults><Blobs>{names}</Blobs></EnumerationResults>")
        if request.method == "GET":
            return httpx.Response(200, content=bytes(self.blobs[name])) if name in self.blobs else httpx.Response(404)
        if request.method == "PUT" and query.get("comp") == ["appendblock"]:
            if name not in self.blobs:
                return httpx.Response(404)
            if int(request.headers["x-ms-blob-condition-appendpos"]) != len(self.blobs[name]):
                return httpx.Response(412, text="AppendPositionConditionNotMet")
            self.blobs[name] += request.content
            return httpx.Response(201)
        if request.method == "PUT":
            assert request.headers["x-ms-blob-type"] == "AppendBlob"
            if name in self.blobs:   # the policy: an existing blob can't be replaced
                return httpx.Response(409, text="BlobImmutableDueToPolicy")
            self.blobs[name] = bytearray()
            return httpx.Response(201)
        if request.method == "DELETE":
            return httpx.Response(409, text="BlobImmutableDueToPolicy")
        return httpx.Response(400)

    def lines(self) -> list[dict]:
        return [json.loads(line) for name in sorted(self.blobs) for line in self.blobs[name].decode().splitlines()]


@pytest.fixture()
def blob():
    return ImmutableContainer()


@pytest.fixture()
def store(tmp_path):
    s = Store(str(tmp_path / "control.db"))
    yield s
    s.close()


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 4, 20, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


def copier(store, blob, clock=None):
    http = httpx.Client(transport=httpx.MockTransport(blob.handle))
    return worm.Copier(store, worm.Container(URL, lambda: "storage-token", http), clock=clock or Clock())


def test_entries_are_copied_once_and_a_restart_carries_on_from_the_copy(store, blob):
    for i in range(3):
        store.append("run_step", {"n": i}, run_id="r1")
    assert copier(store, blob).copy_pending() == 3
    store.append("run_step", {"n": 3}, run_id="r1")
    restarted = copier(store, blob)                  # knows nothing but what is in the copy
    assert restarted.copy_pending() == 1 and restarted.copy_pending() == 0
    assert [e["seq"] for e in blob.lines()] == [1, 2, 3, 4]
    assert blob.lines()[3]["hash"] == store.db.execute("SELECT hash FROM audit WHERE seq = 4").fetchone()["hash"]


def test_a_new_day_starts_a_new_blob(store, blob):
    clock = Clock()
    c = copier(store, blob, clock)
    store.append("run_step", {"n": 1})
    c.copy_pending()
    clock.now = clock.now.replace(day=5)
    store.append("run_step", {"n": 2})
    c.copy_pending()
    assert sorted(blob.blobs) == ["audit-20261104.jsonl", "audit-20261105.jsonl"]
    assert copier(store, blob).compare()["matches"] is True


def test_a_copy_that_moved_on_meanwhile_is_read_again_not_doubled(store, blob):
    c = copier(store, blob)
    store.append("run_step", {"n": 1})
    c.copy_pending()
    store.append("run_step", {"n": 2})
    copier(store, blob).copy_pending()               # another replica got there first
    store.append("run_step", {"n": 3})
    with pytest.raises(httpx.HTTPStatusError):        # stale position: the append is refused
        c.copy_pending()
    assert c.copy_pending() == 1                      # read the copy again, carry on
    assert [e["seq"] for e in blob.lines()] == [1, 2, 3]


def test_a_rewritten_history_passes_the_chain_but_not_the_copy(store, blob, tmp_path):
    for decision in ("approve", "approve", "reject", "approve"):
        store.append("run_step", {"step": "human_review", "decision": decision}, run_id="r1", actor="amira")
    copier(store, blob).copy_pending()
    # Someone with the database turns entry 3's reject into an approval and recomputes the chain from there.
    db = sqlite3.connect(tmp_path / "control.db")
    db.execute("DROP TRIGGER audit_no_update")
    rows = db.execute("SELECT seq, at, type, run_id, actor, payload FROM audit WHERE seq >= 3 ORDER BY seq").fetchall()
    prev = db.execute("SELECT hash FROM audit WHERE seq = 2").fetchone()[0]
    for seq, at, type_, run_id, actor, payload in rows:
        payload = payload.replace("reject", "approve")
        h = entry_hash(prev, seq, at, type_, run_id, actor, payload)
        db.execute("UPDATE audit SET payload = ?, prev_hash = ?, hash = ? WHERE seq = ?", (payload, prev, h, seq))
        prev = h
    db.commit()
    db.close()
    assert store.verify()["valid"] is True            # the chain alone can't tell
    result = copier(store, blob).compare()
    assert result["matches"] is False and result["first_difference"] == 3
    assert result["problem"] == "entry 3 differs from the write-once copy (payload, hash)"


def test_a_deleted_entry_is_caught(store, blob, tmp_path):
    for i in range(3):
        store.append("run_step", {"n": i})
    copier(store, blob).copy_pending()
    db = sqlite3.connect(tmp_path / "control.db")
    db.execute("DROP TRIGGER audit_no_delete")
    db.execute("DELETE FROM audit WHERE seq = 3")
    db.commit()
    db.close()
    assert copier(store, blob).compare()["problem"] == "entry 3 is in the write-once copy but no longer in the database"


def test_the_copy_itself_cannot_be_replaced_or_deleted(store, blob):
    store.append("run_step", {"n": 1})
    copier(store, blob).copy_pending()
    http = httpx.Client(transport=httpx.MockTransport(blob.handle), headers={"Authorization": "Bearer storage-token",
                                                                              "x-ms-version": worm.API_VERSION})
    name = next(iter(blob.blobs))
    assert http.put(f"{URL}/{name}", headers={"x-ms-blob-type": "AppendBlob"}).status_code == 409
    assert http.delete(f"{URL}/{name}").status_code == 409
    assert blob.lines()[0]["seq"] == 1


def test_nothing_to_copy_writes_nothing(store, blob):
    c = copier(store, blob)
    assert c.copy_pending() == 0 and blob.blobs == {}
    assert c.compare() == {"copied_through": 0, "database_through": 0, "copies": 0, "matches": True,
                           "first_difference": None, "problem": None}


def test_the_storage_token_comes_from_the_managed_identity(monkeypatch):
    monkeypatch.setenv("IDENTITY_ENDPOINT", "http://localhost:42356/msi/token")
    monkeypatch.setenv("IDENTITY_HEADER", "platform-secret")
    asked = []

    def handler(request):
        asked.append(dict(request.url.params))
        return httpx.Response(200, json={"access_token": "storage-token", "expires_on": "9999999999"})
    token = worm.ManagedIdentityToken(client_id="control-id", http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert token() == token() == "storage-token" and len(asked) == 1
    assert asked[0] == {"resource": "https://storage.azure.com/", "api-version": "2019-08-01", "client_id": "control-id"}


def test_off_unless_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONTROL_DB", str(tmp_path / "c.db"))
    monkeypatch.setattr(worm, "URL", "")
    from .test_control import CLERK, Keys
    monkeypatch.setattr(auth, "_jwks", Keys())
    with TestClient(main.app) as api:
        assert api.get("/audit/worm", headers=CLERK).json() == {"enabled": False}
        main.state.store.close()


def test_genesis_is_unchanged():
    assert GENESIS == "0" * 64


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_scenario_28_the_tamper_script_rechains_and_only_the_copy_catches_it(backend, blob, tmp_path, monkeypatch,
                                                                             request, capsys):
    from app import tamper
    url = str(tmp_path / "control.db") if backend == "sqlite" else request.getfixturevalue("postgres_db")
    s = Store(url)
    try:
        for decision in ("approve", "approve", "approve"):
            s.append("run_step", {"step": "human_review", "decision": decision}, run_id="r1", actor="amira")
        copier(s, blob).copy_pending()
        monkeypatch.setattr(config, "CONTROL_DB", url)
        monkeypatch.setattr("sys.argv", ["tamper", "--seq", "2", "--rechain"])
        tamper.main()
        assert "Direct UPDATE refused by the trigger" in capsys.readouterr().out
        assert s.verify()["valid"] is True
        assert copier(s, blob).compare()["first_difference"] == 2
    finally:
        s.close()
