"""Scenario 15: tampers with the audit log the way someone with access to the
database file would, so you can watch the chain catch it.

    python -m app.tamper --seq 3

It drops the append-only triggers, rewrites entry 3's payload (turning an
approval into a rejection, or adding a note), and puts the triggers back.
Nothing in the API can do this; it needs the file itself."""
import argparse
import sqlite3

from . import config
from .store import SCHEMA


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--seq", type=int, required=True)
    args = p.parse_args()
    db = sqlite3.connect(config.CONTROL_DB, isolation_level=None)
    row = db.execute("SELECT type, payload FROM audit WHERE seq = ?", (args.seq,)).fetchone()
    if not row:
        raise SystemExit(f"no entry {args.seq}")
    changed = row[1].replace("approve", "reject") if "approve" in row[1] else row[1][:-1] + ', "edited": true}'
    try:
        db.execute("UPDATE audit SET payload = ? WHERE seq = ?", (changed, args.seq))
    except sqlite3.DatabaseError as e:
        print(f"Direct UPDATE refused by the trigger: {e}")
    db.execute("DROP TRIGGER audit_no_update")
    db.execute("UPDATE audit SET payload = ? WHERE seq = ?", (changed, args.seq))
    db.executescript(SCHEMA)  # puts the triggers back, as a careful attacker would
    print(f"Dropped the trigger and rewrote entry {args.seq} ({row[0]}):")
    print(f"  before: {row[1][:150]}")
    print(f"  after:  {changed[:150]}")
    print("Now check the chain: GET /audit/verify, or the console's Audit log page.")


if __name__ == "__main__":
    main()
