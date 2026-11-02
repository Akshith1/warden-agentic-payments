"""warden-policy: the Cedar policy decision point.

Callers say who wants to do what to which payment. Facts about the vendor's
history (known, prior payments, payments in the last 24 hours) are looked up
here, from the ledger, so a caller can't claim a history it doesn't have."""
import hashlib
import os
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .policyset import Rejected, load

POLICY_DIR = os.environ.get("POLICY_DIR", "/policies")
LEDGER_URL = os.environ.get("LEDGER_URL", "http://warden-ledger:8000")
VENDOR_LOOKUP_KEY = os.environ.get("VENDOR_LOOKUP_KEY", "")


WINDOW_SECONDS = 24 * 3600


class State:
    policies = None
    digest = ""
    ledger: httpx.Client
    # Agent approvals this service allowed, per vendor, for the rolling 24-hour limit.
    # The ledger only knows settled payments; without this, a burst of invoices
    # submitted together would all pass before the first one settles. Kept in
    # memory: after a restart the ledger's settled count still applies.
    allowed: dict[str, deque] = defaultdict(deque)
    lock = threading.Lock()


state = State()


def recent_allows(key: str, now: float) -> int:
    q = state.allowed[key]
    while q and q[0] < now - WINDOW_SECONDS:
        q.popleft()
    return len(q)


@asynccontextmanager
async def lifespan(_app):
    try:
        state.policies = load(POLICY_DIR)
    except Rejected as e:
        # Fail closed: a rejected policy set is never served, and with no policy
        # service every payment stops (runs fail and can be retried once fixed).
        raise SystemExit(f"policy set rejected: {e}") from e
    state.digest = hashlib.sha256(state.policies.text.encode()).hexdigest()[:12]
    state.ledger = httpx.Client(base_url=LEDGER_URL, timeout=10)
    yield
    state.ledger.close()


app = FastAPI(title="warden-policy", lifespan=lifespan)


class Principal(BaseModel):
    type: Literal["Agent", "User"]
    id: str = Field(min_length=1, max_length=200)
    # For a person: the roles in their verified Keycloak token, passed on by the backend.
    # Ignored for agents, whose trust tier comes from this service's registry.
    roles: list[str] = Field(default_factory=list, max_length=20)


class PaymentFacts(BaseModel):
    """What the run knows about this payment. Vendor history is not here: the
    service reads it from the ledger."""
    amount: Decimal = Field(ge=0)
    currency: str
    vendor: str = Field(min_length=1)
    confidence: float = Field(default=0, ge=0, le=1)
    critic_agrees: bool = False
    remit_changed: bool = False
    submitted_by: str = ""  # username of the person who submitted the invoice, if any


class AuthorizeRequest(BaseModel):
    principal: Principal
    action: Literal["ApprovePayment", "DryRunLedgerEntry", "CreateLedgerEntry", "RecordVendorPayment",
                    "LookupVendor", "ConfirmVendorAlias"]
    payment: PaymentFacts | None = None
    vendor: str | None = None  # for LookupVendor and ConfirmVendorAlias


def vendor_history(name: str) -> dict:
    r = state.ledger.get("/vendors/lookup", params={"name": name}, headers={"X-API-Key": VENDOR_LOOKUP_KEY})
    r.raise_for_status()
    return r.json()


@app.get("/health")
def health():
    return {"status": "ok", "policy_set": state.digest, "rules": len(state.policies.rules)}


@app.get("/policies")
def policies():
    """The rules in force, for the console's governance page."""
    return {
        "digest": state.digest,
        "files": state.policies.files,
        "rules": [{"id": r.id, "effect": r.effect, "reason": r.reason, "summary": r.summary,
                   "file": r.file}
                  for r in state.policies.rules.values()],
    }


@app.post("/authorize")
def authorize(req: AuthorizeRequest):
    if req.payment:
        try:
            history = vendor_history(req.payment.vendor)
        except httpx.HTTPError as e:  # no history, no decision: fail closed
            raise HTTPException(status_code=503, detail=f"vendor history unavailable: {e}") from e
        facts = {
            "amountCents": int((req.payment.amount * 100).to_integral_value()),
            "currency": req.payment.currency.upper(),
            "vendor": history["vendor_name"],
            "vendorKnown": history["known"],
            "priorPayments": history["payment_count"],
            "paymentsLast24h": history.get("payments_last_24h", 0),  # raised below by allows in flight
            "confidencePct": int(round(req.payment.confidence * 100)),
            "criticAgrees": req.payment.critic_agrees,
            "remitChanged": req.payment.remit_changed,
            "submittedBy": req.payment.submitted_by,
        }
        resource = {"type": "Payment", "id": "payment", "attrs": facts}
    elif req.vendor:
        facts = {"name": req.vendor}
        resource = {"type": "Vendor", "id": "vendor", "attrs": facts}
    else:
        raise HTTPException(status_code=422, detail="give either payment or vendor")
    counts_toward_limit = req.payment and req.action == "ApprovePayment" and req.principal.type == "Agent"
    if not counts_toward_limit:
        result = state.policies.authorize(req.principal.model_dump(), req.action, resource)
        return result | {"facts": facts, "policy_set": state.digest}

    key = f"id:{history['vendor_id']}" if history.get("vendor_id") else f"name:{history['vendor_name'].upper()}"
    with state.lock:  # count, decide and record as one step, so two calls can't both take the last slot
        now = time.time()
        facts["paymentsLast24h"] = max(facts["paymentsLast24h"], recent_allows(key, now))
        result = state.policies.authorize(req.principal.model_dump(), req.action, resource)
        if result["decision"] == "Allow":
            state.allowed[key].append(now)
    return result | {"facts": facts, "policy_set": state.digest}
