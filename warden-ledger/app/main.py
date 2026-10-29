import os
import secrets
from contextlib import asynccontextmanager
from decimal import Decimal

from fastapi import Depends, FastAPI, Header, HTTPException

from . import db
from .ledger import apply_transaction
from .models import Posting, RecordPaymentRequest, TransactionRequest, ValidationResult, VendorLookup

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
        vendor = conn.execute(
            "SELECT id, display_name, first_seen FROM vendors WHERE normalized_name = %s",
            (db.normalize_vendor(name),),
        ).fetchone()
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
            first_seen=vendor["first_seen"],
            payment_count=stats["n"],
            average_amount=round(stats["avg"], 2) if stats["avg"] is not None else None,
            recent_amounts=[r["amount"] for r in recent],
            last_remit_account=last_remit,
        )


@app.post("/vendors/record-payment", dependencies=[Depends(require_write)])
def record_payment(req: RecordPaymentRequest):
    with db.connect() as conn:
        vendor_id = upsert_vendor(conn, req.vendor_name, req.paid_on)
        inserted = conn.execute(
            """INSERT INTO historical_payments (vendor_id, amount, paid_on, source, external_ref, remit_account)
               VALUES (%s, %s, %s, 'warden', %s, %s) ON CONFLICT (external_ref) DO NOTHING RETURNING id""",
            (vendor_id, req.amount, req.paid_on, req.external_ref, req.remit_account),
        ).fetchone()
        conn.commit()
    return {"vendor_id": vendor_id, "recorded": inserted is not None}


def upsert_vendor(conn, name: str, seen_on) -> int:
    return conn.execute(
        """INSERT INTO vendors (normalized_name, display_name, first_seen) VALUES (%s, %s, %s)
           ON CONFLICT (normalized_name) DO UPDATE SET first_seen = LEAST(vendors.first_seen, EXCLUDED.first_seen)
           RETURNING id""",
        (db.normalize_vendor(name), name.strip(), seen_on),
    ).fetchone()["id"]
