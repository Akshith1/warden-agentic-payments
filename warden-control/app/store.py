"""The control plane's storage: the kill switch and the audit log, in SQLite,
or (phase 11, Azure) in Postgres when CONTROL_DB is a postgresql:// URL: a
container on Container Apps has no lasting disk of its own.

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
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

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
CREATE TABLE IF NOT EXISTS spend (
    at                 TEXT NOT NULL,
    day                TEXT NOT NULL,
    agent              TEXT NOT NULL,
    route              TEXT,
    prompt_tokens      INTEGER NOT NULL,
    completion_tokens  INTEGER NOT NULL,
    cost_usd           REAL
);
CREATE INDEX IF NOT EXISTS spend_agent_day ON spend (agent, day);
CREATE TABLE IF NOT EXISTS switches (
    scope      TEXT NOT NULL,
    target     TEXT NOT NULL,
    reason     TEXT NOT NULL,
    by         TEXT NOT NULL,
    since      TEXT NOT NULL,
    PRIMARY KEY (scope, target)
);
"""

# The same tables in Postgres. seq is still assigned by append(), under a lock, so the chain
# is computed exactly as in SQLite.
PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    seq        BIGINT PRIMARY KEY,
    at         TEXT NOT NULL,
    type       TEXT NOT NULL,
    run_id     TEXT,
    actor      TEXT,
    payload    TEXT NOT NULL,
    prev_hash  TEXT NOT NULL,
    hash       TEXT NOT NULL
);
CREATE OR REPLACE FUNCTION audit_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'the audit log is append-only'; END $$;
DROP TRIGGER IF EXISTS audit_no_change ON audit;
CREATE TRIGGER audit_no_change BEFORE UPDATE OR DELETE ON audit
FOR EACH ROW EXECUTE FUNCTION audit_append_only();
CREATE TABLE IF NOT EXISTS spend (
    at                 TEXT NOT NULL,
    day                TEXT NOT NULL,
    agent              TEXT NOT NULL,
    route              TEXT,
    prompt_tokens      INTEGER NOT NULL,
    completion_tokens  INTEGER NOT NULL,
    cost_usd           DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS spend_agent_day ON spend (agent, day);
CREATE TABLE IF NOT EXISTS switches (
    scope      TEXT NOT NULL,
    target     TEXT NOT NULL,
    reason     TEXT NOT NULL,
    "by"       TEXT NOT NULL,
    since      TEXT NOT NULL,
    PRIMARY KEY (scope, target)
);
"""


class _Sqlite:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def execute(self, sql: str, params=()):
        return self.db.execute(sql, params)

    @contextmanager
    def transaction(self):
        yield self

    def close(self):
        self.db.close()


class _Postgres:
    """A small pool, so a connection Azure closed while idle is replaced, not reused."""

    def __init__(self, url: str):
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
        self.pool = ConnectionPool(url, min_size=1, max_size=4, open=True, check=ConnectionPool.check_connection,
                                   kwargs={"autocommit": True, "row_factory": dict_row})
        self.pool.wait(timeout=60)
        with self.pool.connection() as conn:
            conn.execute("SELECT pg_advisory_lock(7411)")   # two replicas starting at once take turns
            try:
                conn.execute(PG_SCHEMA)
            finally:
                conn.execute("SELECT pg_advisory_unlock(7411)")
        self._local = threading.local()

    def execute(self, sql: str, params=()):
        sql = sql.replace("?", "%s")
        conn = getattr(self._local, "conn", None)
        if conn is not None:                             # inside this thread's transaction()
            return conn.execute(sql, params)
        with self.pool.connection() as conn:
            cur = conn.execute(sql, params)
            rows = cur.fetchall() if cur.description else []
            return _Result(rows, cur.rowcount)

    def close(self):
        self.pool.close()

    @contextmanager
    def transaction(self):
        """append() runs here: one connection, with a lock that also holds across replicas."""
        with self.pool.connection() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(7412)")
            self._local.conn = conn
            try:
                yield self
            finally:
                self._local.conn = None


class _Result:
    def __init__(self, rows, rowcount):
        self._rows, self.rowcount = rows, rowcount

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def __iter__(self):
        return iter(self._rows)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def entry_hash(prev_hash: str, seq: int, at: str, type_: str, run_id, actor, payload: str) -> str:
    body = json.dumps({"seq": seq, "at": at, "type": type_, "run_id": run_id, "actor": actor, "payload": payload},
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((prev_hash + body).encode()).hexdigest()


class Store:
    def __init__(self, path: str):
        self.db = _Postgres(path) if path.startswith(("postgresql://", "postgres://")) else _Sqlite(path)
        self.lock = threading.Lock()

    def close(self) -> None:
        self.db.close()

    # ---- audit log ----
    def append(self, type_: str, payload: dict, run_id: str | None = None, actor: str | None = None) -> dict:
        text = json.dumps(payload, sort_keys=True, default=str)
        with self.lock, self.db.transaction() as db:
            last = db.execute("SELECT seq, hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
            seq, prev = (last["seq"] + 1, last["hash"]) if last else (1, GENESIS)
            at = now()
            h = entry_hash(prev, seq, at, type_, run_id, actor, text)
            db.execute("INSERT INTO audit VALUES (?,?,?,?,?,?,?,?)", (seq, at, type_, run_id, actor, text, prev, h))
        return {"seq": seq, "hash": h}

    def entries(self, limit: int = 100, run_id: str | None = None) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM audit WHERE (CAST(? AS TEXT) IS NULL OR run_id = ?) ORDER BY seq DESC LIMIT ?",
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

    # ---- spend per agent (phase 8) ----
    def add_spend(self, agent: str, route: str | None, prompt_tokens: int, completion_tokens: int,
                  cost_usd: float | None, at: str) -> None:
        day = datetime.fromisoformat(at).astimezone(timezone.utc).date().isoformat()
        with self.lock:
            self.db.execute("INSERT INTO spend VALUES (?,?,?,?,?,?,?)",
                            (at, day, agent, route, prompt_tokens, completion_tokens, cost_usd))

    def spend_today(self, agent: str) -> dict:
        day = datetime.now(timezone.utc).date().isoformat()
        r = self.db.execute("SELECT count(*) AS calls, coalesce(sum(prompt_tokens + completion_tokens), 0) AS tokens, "
                            "sum(cost_usd) AS cost FROM spend WHERE agent = ? AND day = ?", (agent, day)).fetchone()
        return {"agent": agent, "day": day, "calls": r["calls"], "tokens": r["tokens"], "cost_usd": r["cost"]}

    def spend_by_day(self, days: int = 7) -> list[dict]:
        rows = self.db.execute(
            """SELECT day, agent, route, count(*) AS calls, sum(prompt_tokens) AS prompt_tokens,
                      sum(completion_tokens) AS completion_tokens, sum(cost_usd) AS cost_usd
               FROM spend WHERE day >= ? GROUP BY day, agent, route ORDER BY day DESC, agent""",
            ((datetime.now(timezone.utc).date() - timedelta(days=days - 1)).isoformat(),)).fetchall()
        return [dict(r) for r in rows]
