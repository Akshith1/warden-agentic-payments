"""The control plane's storage: the kill switch and the audit log, in SQLite.

The audit log is a hash chain. Each entry's hash covers its own content and
the previous entry's hash, so changing or deleting any past entry changes
every hash after it. `verify()` recomputes the chain and names the first entry
that no longer matches. Triggers also refuse UPDATE and DELETE on the table;
someone with raw access to the file can drop them, which is exactly the case
the chain is there to catch."""
import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone

GENESIS = "0" * 64
SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    seq        INTEGER PRIMARY KEY,
    at         TEXT NOT NULL,
    type       TEXT NOT NULL,
    run_id     TEXT,
    actor      TEXT,
    payload    TEXT NOT NULL,
    prev_hash  TEXT NOT NULL,
    hash       TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
BEGIN SELECT RAISE(ABORT, 'the audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
BEGIN SELECT RAISE(ABORT, 'the audit log is append-only'); END;
CREATE TABLE IF NOT EXISTS switches (
    scope      TEXT NOT NULL,
    target     TEXT NOT NULL,
    reason     TEXT NOT NULL,
    by         TEXT NOT NULL,
    since      TEXT NOT NULL,
    PRIMARY KEY (scope, target)
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def entry_hash(prev_hash: str, seq: int, at: str, type_: str, run_id, actor, payload: str) -> str:
    body = json.dumps({"seq": seq, "at": at, "type": type_, "run_id": run_id, "actor": actor, "payload": payload},
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((prev_hash + body).encode()).hexdigest()


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()

    # ---- audit log ----
    def append(self, type_: str, payload: dict, run_id: str | None = None, actor: str | None = None) -> dict:
        text = json.dumps(payload, sort_keys=True, default=str)
        with self.lock:
            last = self.db.execute("SELECT seq, hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
            seq, prev = (last["seq"] + 1, last["hash"]) if last else (1, GENESIS)
            at = now()
            h = entry_hash(prev, seq, at, type_, run_id, actor, text)
            self.db.execute("INSERT INTO audit VALUES (?,?,?,?,?,?,?,?)", (seq, at, type_, run_id, actor, text, prev, h))
        return {"seq": seq, "hash": h}

    def entries(self, limit: int = 100, run_id: str | None = None) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM audit WHERE (? IS NULL OR run_id = ?) ORDER BY seq DESC LIMIT ?",
            (run_id, run_id, limit)).fetchall()
        return [dict(r) | {"payload": json.loads(r["payload"])} for r in rows]

    def verify(self) -> dict:
        prev, count = GENESIS, 0
        expected_seq = 1
        for r in self.db.execute("SELECT * FROM audit ORDER BY seq"):
            if r["seq"] != expected_seq:
                return {"valid": False, "entries": count, "broken_at": expected_seq,
                        "problem": f"entry {expected_seq} is missing"}
            if r["prev_hash"] != prev:
                return {"valid": False, "entries": count, "broken_at": r["seq"],
                        "problem": f"entry {r['seq']} doesn't follow entry {r['seq'] - 1}"}
            if entry_hash(prev, r["seq"], r["at"], r["type"], r["run_id"], r["actor"], r["payload"]) != r["hash"]:
                return {"valid": False, "entries": count, "broken_at": r["seq"],
                        "problem": f"entry {r['seq']} was changed after it was written"}
            prev, count, expected_seq = r["hash"], count + 1, expected_seq + 1
        return {"valid": True, "entries": count, "head": prev}

    # ---- kill switch ----
    def trip(self, scope: str, target: str, reason: str, by: str) -> bool:
        with self.lock:
            existing = self.db.execute("SELECT 1 FROM switches WHERE scope=? AND target=?", (scope, target)).fetchone()
            if existing:
                return False
            self.db.execute("INSERT INTO switches VALUES (?,?,?,?,?)", (scope, target, reason, by, now()))
        return True

    def reset(self, scope: str, target: str) -> bool:
        with self.lock:
            return self.db.execute("DELETE FROM switches WHERE scope=? AND target=?", (scope, target)).rowcount > 0

    def status(self) -> dict:
        rows = [dict(r) for r in self.db.execute("SELECT * FROM switches ORDER BY since")]
        global_stop = next((r for r in rows if r["scope"] == "global"), None)
        return {"global_stop": global_stop is not None, "global": global_stop,
                "paused_agents": [r["target"] for r in rows if r["scope"] == "agent"],
                "switches": rows}

    def recent(self, type_: str, minutes: int) -> list[dict]:
        cutoff = datetime.now(timezone.utc).timestamp() - minutes * 60
        rows = self.db.execute("SELECT * FROM audit WHERE type = ? ORDER BY seq DESC LIMIT 500", (type_,)).fetchall()
        return [dict(r) | {"payload": json.loads(r["payload"])} for r in rows
                if datetime.fromisoformat(r["at"]).timestamp() >= cutoff]
