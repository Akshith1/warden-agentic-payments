import json

from .. import config
from ..schemas import ApprovalDecision, RunState
from . import Deps, audit, prompt


def rules_context() -> dict:
    """Tells the model which rules code has already applied, so it judges the
    evidence instead of re-deciding policy (the phase 1 live run escalated on
    exactly these two points before they were written down)."""
    return {
        "ledger_currency": config.LEDGER_CURRENCY,
        "currency_rule": "A document that shows no currency is in the ledger currency (currency_assumed=true). "
                         "Other currencies are escalated by code before you see them.",
        "minimum_prior_payments": config.MIN_PRIOR_PAYMENTS,
        "history_rule": "Vendors with fewer settled payments than the minimum are escalated by code before you "
                        "see them; you only see vendors that meet it.",
    }


def run(state: RunState, deps: Deps) -> dict:
    ext = state["extraction"]
    evidence = json.dumps({"invoice": ext, "vendor_history": state["vendor"], "risk": state["risk"],
                           "rules_already_enforced": rules_context()}, default=str, indent=2)
    decision = deps.llm("approval", config.ROUTE_REASONING, prompt("approval"), evidence, ApprovalDecision)

    trail = audit("approval", decision=decision.decision, confidence=decision.confidence)
    if decision.decision == "reject":
        return {"approval": decision.model_dump(), "status": "rejected",
                "status_reason": "; ".join(decision.reasons), "audit_trail": trail}

    # Code-level guards. Phase 4 adds Cedar as an independent second check of the same rules.
    reasons = list(state.get("escalation_reasons", []))
    if ext["total_amount"] > config.ROUTINE_AMOUNT_THRESHOLD:
        reasons.append(f"amount above ${config.ROUTINE_AMOUNT_THRESHOLD:,.0f} needs a human approver")
    if decision.confidence < config.CONFIDENCE_THRESHOLD:
        reasons.append(f"decision confidence {decision.confidence:.2f} below {config.CONFIDENCE_THRESHOLD}")
    if decision.decision == "escalate":
        reasons.append("approval agent escalated: " + "; ".join(decision.reasons))

    return {"approval": decision.model_dump(), "escalation_reasons": reasons, "audit_trail": trail}
