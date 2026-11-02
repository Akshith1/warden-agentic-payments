"""Deterministic on purpose: no model output is consulted at the moment money
moves. The amount, vendor and account come from state the earlier agents and
the human already reviewed. Two nodes, so the run shows `settling` for the
whole hold window rather than looking finished while money has not moved."""
import re
from datetime import date

from .. import config
from ..schemas import RunState
from . import Deps, audit


def idempotency_key(state: RunState) -> str:
    ext = state["extraction"]
    if ext.get("invoice_number"):
        # The ledger's name for the vendor when it has one, so the same invoice read under
        # a confirmed alias still collides with the first payment instead of paying twice.
        name = (state.get("vendor") or {}).get("vendor_name") or ext["vendor_name"]
        vendor = re.sub(r"[^A-Z0-9]+", "-", name.upper()).strip("-")
        number = re.sub(r"[^A-Z0-9]+", "-", ext["invoice_number"].upper()).strip("-")
        return f"inv:{vendor}:{number}"  # the same invoice submitted twice collides here
    return f"run:{state['run_id']}"


def _transaction(state: RunState) -> dict:
    ext = state["extraction"]
    amount = f"{ext['total_amount']:.2f}"
    return {
        "idempotency_key": idempotency_key(state),
        "description": f"{ext['vendor_name']} invoice {ext.get('invoice_number') or '(no number)'}",
        "postings": [
            {"account_id": config.EXPENSE_ACCOUNT, "direction": "debit", "amount": amount},
            {"account_id": config.CASH_ACCOUNT, "direction": "credit", "amount": amount},
        ],
    }


def dry_run(state: RunState, deps: Deps) -> dict:
    txn = _transaction(state)
    check = deps.ledger.dry_run(txn)
    if not check["valid"]:
        return {"status": "blocked", "status_reason": "dry run failed: " + "; ".join(check["errors"]),
                "payment": {"dry_run": check}, "audit_trail": audit("dry_run", valid=False, errors=check["errors"])}
    return {"status": "settling", "payment": {"dry_run": check},
            "audit_trail": audit("dry_run", valid=True, resulting_balances=check["resulting_balances"])}


def settle(state: RunState, deps: Deps) -> dict:
    trail = audit("settlement_hold", seconds=config.SETTLEMENT_HOLD_SECONDS)
    deps.sleep(config.SETTLEMENT_HOLD_SECONDS)  # phase 9 re-checks the kill switch here, right before commit

    txn = _transaction(state)
    ok, result = deps.ledger.commit(txn)
    if not ok:
        return {"status": "blocked", "status_reason": "commit refused: " + "; ".join(result),
                "audit_trail": trail + audit("commit", ok=False, errors=result)}

    # Vendor history is written only after a real settlement, so trust is earned from the ledger.
    ext = state["extraction"]
    deps.ledger.record_payment({
        "vendor_name": ext["vendor_name"], "amount": txn["postings"][0]["amount"],
        "paid_on": date.today().isoformat(), "external_ref": txn["idempotency_key"],
        "remit_account": ext.get("remit_to_account"),
    })
    return {
        "status": "settled",
        "payment": state["payment"] | {"transaction_id": result["transaction_id"],
                                       "idempotency_key": txn["idempotency_key"],
                                       "amount": txn["postings"][0]["amount"]},
        "audit_trail": trail + audit("commit", ok=True, transaction_id=result["transaction_id"]),
    }
