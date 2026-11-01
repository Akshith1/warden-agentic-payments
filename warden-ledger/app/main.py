import os
import secrets
from contextlib import asynccontextmanager
from decimal import Decimal

from fastapi import Depends, FastAPI, Header, HTTPException

from . import db
from .ledger import apply_transaction
from .models import (AliasRequest, Posting, RecordPaymentRequest, TransactionRequest, ValidationResult,
                     VendorCandidate, VendorLookup)

WRITE_KEY = os.environ["LEDGER_WRITE_KEY"]
LOOKUP_KEY = os.environ["VENDOR_LOOKUP_KEY"]
OPENING_CASH = Decimal(os.environ.get("OPENING_CASH", "250000.00"))


def _require(expected: str):
    def check(x_api_key: str = Header(default="")):
        if not secrets.compare_digest(x_api_key, expected):
            raise HTTPException(status_code=401, detail="invalid API key")
    return check


require_write = _require(WRITE_KEY)
require_read = _require(LOOKUP_KEY)


@asynccontextmanager
async def lifespan(_app):
    db.wait_for_db()
    with db.connect() as conn:
        db.init_schema(conn)
        if OPENING_CASH > 0:
            apply_transaction(conn, TransactionRequest(
                idempotency_key="opening-balance",
                description="Opening cash balance",
                postings=[
                    Posting(account_id="1000-operating-cash", direction="debit", amount=OPENING_CASH),
                    Posting(account_id="3000-opening-equity", direction="credit", amount=OPENING_CASH),
                ],
            ))  # a second start sees the duplicate key and writes nothing
    yield


app = FastAPI(title="warden-ledger", lifespan=lifespan)


@app.get("/health")
def health():
    with db.connect() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.post("/transactions/dry-run", response_model=ValidationResult, dependencies=[Depends(require_write)])
def dry_run(req: TransactionRequest):
    with db.connect() as conn:
        result = apply_transaction(conn, req)
        conn.rollback()
    result.transaction_id = None
    return result


@app.post("/transactions", response_model=ValidationResult, dependencies=[Depends(require_write)])
def commit(req: TransactionRequest):
    with db.connect() as conn:
        result = apply_transaction(conn, req)
        if not result.valid:
            conn.rollback()
            status = 409 if any(e.startswith("duplicate idempotency key") for e in result.errors) else 422
            raise HTTPException(status_code=status, detail=result.errors)
        conn.commit()
    return result


@app.get("/accounts", dependencies=[Depends(require_read)])
def accounts():
    with db.connect() as conn:
        return conn.execute("SELECT id, name, type, balance FROM accounts ORDER BY id").fetchall()


@app.get("/transactions", dependencies=[Depends(require_read)])
def transactions(limit: int = 50):
    with db.connect() as conn:
        return conn.execute(
            """SELECT t.id, t.idempotency_key, t.description, t.created_at,
                      json_agg(json_build_object('account_id', p.account_id, 'direction', p.direction,
                                                 'amount', p.amount) ORDER BY p.id) AS postings
               FROM transactions t JOIN postings p ON p.transaction_id = t.id
               GROUP BY t.id ORDER BY t.id DESC LIMIT %s""",
            (min(limit, 500),),
        ).fetchall()


@app.get("/vendors/lookup", response_model=VendorLookup, dependencies=[Depends(require_read)])
def vendor_lookup(name: str):
    with db.connect() as conn:
        vendor = db.resolve_vendor(conn, name)
        if not vendor:
            return VendorLookup(known=False, vendor_name=name)
        stats = conn.execute(
            "SELECT count(*) AS n, avg(amount) AS avg FROM historical_payments WHERE vendor_id = %s",
            (vendor["id"],),
        ).fetchone()
        recent = conn.execute(
            "SELECT amount, remit_account FROM historical_payments WHERE vendor_id = %s "
            "ORDER BY paid_on DESC, id DESC LIMIT 5",
            (vendor["id"],),
        ).fetchall()
        last_remit = next((r["remit_account"] for r in recent if r["remit_account"]), None)
        return VendorLookup(
            known=True,
            vendor_name=vendor["display_name"],
            vendor_id=vendor["id"],
            matched_alias=vendor["matched_alias"],
            first_seen=vendor["first_seen"],
            payment_count=stats["n"],
            average_amount=round(stats["avg"], 2) if stats["avg"] is not None else None,
            recent_amounts=[r["amount"] for r in recent],
            last_remit_account=last_remit,
        )


@app.post("/vendors/record-payment", dependencies=[Depends(require_write)])
def record_payment(req: RecordPaymentRequest):
    with db.connect() as conn:
        known = db.resolve_vendor(conn, req.vendor_name)  # a confirmed alias pays into the vendor's history
        vendor_id = known["id"] if known else upsert_vendor(conn, req.vendor_name, req.paid_on)
        inserted = conn.execute(
            """INSERT INTO historical_payments (vendor_id, amount, paid_on, source, external_ref, remit_account)
               VALUES (%s, %s, %s, 'warden', %s, %s) ON CONFLICT (external_ref) DO NOTHING RETURNING id""",
            (vendor_id, req.amount, req.paid_on, req.external_ref, req.remit_account),
        ).fetchone()
        conn.commit()
    return {"vendor_id": vendor_id, "recorded": inserted is not None}


# Words almost every vendor shares; matching on them alone lists unrelated vendors.
COMPANY_FORMS = {"SDN", "BHD", "LLC", "INC", "LTD", "LIMITED", "CORP", "CORPORATION", "COMPANY", "PTE", "PLC",
                 "THE", "AND", "ENTERPRISE", "ENTERPRISES", "TRADING", "SERVICES", "GROUP"}


@app.get("/vendors/search", response_model=list[VendorCandidate], dependencies=[Depends(require_read)])
def vendor_search(q: str, limit: int = 8):
    """Candidates for a person to choose from. This only lists; it never links
    anything, so a loose match here can't move money on its own."""
    words = [w for w in db.normalize_vendor(q).split() if len(w) >= 3 and w not in COMPANY_FORMS][:8]
    if not words:
        return []
    patterns = [f"%{w}%" for w in words]
    with db.connect() as conn:
        rows = conn.execute(
            """WITH names AS (
                   SELECT id AS vendor_id, normalized_name AS name FROM vendors
                   UNION ALL SELECT vendor_id, normalized_alias FROM vendor_aliases),
               scored AS (
                   SELECT vendor_id, max((SELECT count(*) FROM unnest(%s::text[]) p WHERE name LIKE p)) AS hits
                   FROM names GROUP BY vendor_id)
               SELECT v.id AS vendor_id, v.display_name AS vendor_name,
                      (SELECT count(*) FROM historical_payments h WHERE h.vendor_id = v.id) AS payment_count,
                      (SELECT h.remit_account FROM historical_payments h WHERE h.vendor_id = v.id
                         AND h.remit_account IS NOT NULL ORDER BY paid_on DESC, id DESC LIMIT 1) AS last_remit_account,
                      COALESCE((SELECT array_agg(display_alias ORDER BY confirmed_at) FROM vendor_aliases a
                                WHERE a.vendor_id = v.id), '{}') AS aliases
               FROM scored s JOIN vendors v ON v.id = s.vendor_id
               WHERE s.hits > 0 ORDER BY s.hits DESC, payment_count DESC, v.display_name LIMIT %s""",
            (patterns, min(limit, 25)),
        ).fetchall()
    return rows


@app.post("/vendors/aliases", status_code=201, dependencies=[Depends(require_write)])
def confirm_alias(req: AliasRequest):
    """Records that a person confirmed `alias` is another name for an existing
    vendor. Repeating the same confirmation is harmless; a conflicting one is 409."""
    key = db.normalize_vendor(req.alias)
    if not key:
        raise HTTPException(status_code=422, detail="alias has no letters or digits")
    with db.connect() as conn:
        target = conn.execute("SELECT id, display_name FROM vendors WHERE id = %s", (req.vendor_id,)).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="no such vendor")
        existing = db.resolve_vendor(conn, req.alias)
        if existing and existing["id"] != req.vendor_id:
            raise HTTPException(status_code=409, detail=f"'{req.alias}' already belongs to {existing['display_name']}")
        if existing:
            return {"vendor_id": req.vendor_id, "vendor_name": target["display_name"], "created": False}
        conn.execute(
            """INSERT INTO vendor_aliases (normalized_alias, vendor_id, display_alias, confirmed_by, run_ref)
               VALUES (%s, %s, %s, %s, %s)""",
            (key, req.vendor_id, req.alias.strip(), req.confirmed_by.strip(), req.run_ref),
        )
        conn.commit()
    return {"vendor_id": req.vendor_id, "vendor_name": target["display_name"], "created": True}


def upsert_vendor(conn, name: str, seen_on) -> int:
    return conn.execute(
        """INSERT INTO vendors (normalized_name, display_name, first_seen) VALUES (%s, %s, %s)
           ON CONFLICT (normalized_name) DO UPDATE SET first_seen = LEAST(vendors.first_seen, EXCLUDED.first_seen)
           RETURNING id""",
        (db.normalize_vendor(name), name.strip(), seen_on),
    ).fetchone()["id"]
