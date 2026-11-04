"""Scenarios 15 and 28: tampers with the audit log the way someone with access to
the database would, so you can watch it get caught.

    python -m app.tamper --seq 3              # scenario 15: the chain catches it
    python -m app.tamper --seq 3 --rechain    # scenario 28: only the write-once copy does

It switches off the append-only trigger, rewrites entry 3's payload (turning an
approval into a rejection, or adding a note), and switches the trigger back on.
With --rechain it also recomputes every fingerprint from entry 3 onwards, as a
careful insider would: the chain then verifies, and the edit shows only against
the write-once copy in Blob storage (phase 11b). Nothing in the API can do
this; it needs the database itself. Works on SQLite (locally) and Postgres
(on Azure: `az containerapp exec -n warden-control -g warden-rg --command ...`)."""
import argparse
import sqlite3

from . import config
from .store import SCHEMA, entry_hash


class _Db:
    def __init__(self, url: str):
        self.pg = url.startswith(("postgresql://", "postgres://"))
        if self.pg:
            import psycopg
            self.conn = psycopg.connect(url, autocommit=True)
        else:
            self.conn = sqlite3.connect(url, isolation_level=None)

    def q(self, sql: str, params=()):
        return self.conn.execute(sql.replace("?", "%s") if self.pg else sql, params)

    def triggers_off(self):
        if self.pg:
            self.q("ALTER TABLE audit DISABLE TRIGGER audit_no_change")
        else:
            self.q("DROP TRIGGER audit_no_update")

    def triggers_on(self):
        if self.pg:
            self.q("ALTER TABLE audit ENABLE TRIGGER audit_no_change")
        else:
            self.conn.executescript(SCHEMA)   # puts the triggers back, as a careful attacker would


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--seq", type=int, required=True)
    p.add_argument("--rechain", action="store_true", help="recompute the fingerprints after the edit too")
    args = p.parse_args()
    db = _Db(config.CONTROL_DB)
    row = db.q("SELECT type, payload FROM audit WHERE seq = ?", (args.seq,)).fetchone()
    if not row:
        raise SystemExit(f"no entry {args.seq}")
    changed = row[1].replace("approve", "reject") if "approve" in row[1] else row[1][:-1] + ', "edited": true}'
    try:
        db.q("UPDATE audit SET payload = ? WHERE seq = ?", (changed, args.seq))
    except Exception as e:  # noqa: BLE001 - the trigger's refusal, shown
        print(f"Direct UPDATE refused by the trigger: {str(e).splitlines()[0]}")
    db.triggers_off()
    try:
        db.q("UPDATE audit SET payload = ? WHERE seq = ?", (changed, args.seq))
        if args.rechain:
            prev = db.q("SELECT hash FROM audit WHERE seq = ?", (args.seq - 1,)).fetchone()
            prev = prev[0] if prev else "0" * 64
            rows = db.q("SELECT seq, at, type, run_id, actor, payload FROM audit WHERE seq >= ? ORDER BY seq",
                        (args.seq,)).fetchall()
            for seq, at, type_, run_id, actor, payload in rows:
                h = entry_hash(prev, seq, at, type_, run_id, actor, payload)
                db.q("UPDATE audit SET prev_hash = ?, hash = ? WHERE seq = ?", (prev, h, seq))
                prev = h
    finally:
        db.triggers_on()
    print(f"Switched off the trigger and rewrote entry {args.seq} ({row[0]}):")
    print(f"  before: {row[1][:150]}")
    print(f"  after:  {changed[:150]}")
    if args.rechain:
        print(f"Recomputed the fingerprints of entries {args.seq} onwards: the chain will verify.")
    print("Now look at the console's Audit log page.")


if __name__ == "__main__":
    main()
