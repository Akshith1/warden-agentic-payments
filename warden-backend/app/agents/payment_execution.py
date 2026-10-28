"""Deterministic on purpose: no model output is consulted at the moment money
moves. The amount, vendor and account come from state the earlier agents and
the human already reviewed."""
import re
from datetime import date

from .. import config
from ..schemas import RunState
from . import Deps, audit


def idempotency_key(state: RunState) -> str:
    ext = state["extraction"]
    if ext.get("invoice_number"):
        vendor = re.sub(r"[^A-Z0-9]+", "-", ext["vendor_name"].upper()).strip("-")
        number = re.sub(r"[^A-Z0-9]+", "-", ext["invoice_number"].upper()).strip("-")
        return f"inv:{vendor}:{number}"  # the same invoice submitted twice collides here
    return f"run:{state['run_id']}"


def run(state: RunState, deps: Deps) -> dict:
    ext = state["extraction"]
    amount = f"{ext['total_amount']:.2f}"
    key = idempotency_key(state)
    txn = {
        "idempotency_key": key,
        "description": f"{ext['vendor_name']} invoice {ext.get('invoice_number') or '(no number)'}",
        "postings": [
            {"account_id": config.EXPENSE_ACCOUNT, "direction": "debit", "amount": amount},
            {"account_id": config.CASH_ACCOUNT, "direction": "credit", "amount": amount},
        ],
    }

    check = deps.ledger.dry_run(txn)
    if not check["valid"]:
        return {"status": "blocked", "status_reason": "dry run failed: " + "; ".join(check["errors"]),
                "payment": {"dry_run": check}, "audit_trail": audit("dry_run", valid=False, errors=check["errors"])}

    trail = audit("dry_run", valid=True, resulting_balances=check["resulting_balances"])
    trail += audit("settlement_hold", seconds=config.SETTLEMENT_HOLD_SECONDS)
    deps.sleep(config.SETTLEMENT_HOLD_SECONDS)

    ok, result = deps.ledger.commit(txn)
    if not ok:
        return {"status": "blocked", "status_reason": "commit refused: " + "; ".join(result),
                "payment": {"dry_run": check}, "audit_trail": trail + audit("commit", ok=False, errors=result)}

    # Vendor history is written only after a real settlement, so trust is earned from the ledger.
    deps.ledger.record_payment({
        "vendor_name": ext["vendor_name"], "amount": amount, "paid_on": date.today().isoformat(),
        "external_ref": key, "remit_account": ext.get("remit_to_account"),
    })
    return {
        "status": "settled",
        "payment": {"transaction_id": result["transaction_id"], "idempotency_key": key, "amount": amount},
        "audit_trail": trail + audit("commit", ok=True, transaction_id=result["transaction_id"]),
    }
