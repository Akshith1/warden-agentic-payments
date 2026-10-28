import json

from .. import config
from ..schemas import ApprovalDecision, RunState
from . import Deps, audit, prompt


def run(state: RunState, deps: Deps) -> dict:
    ext = state["extraction"]
    evidence = json.dumps({"invoice": ext, "vendor_history": state["vendor"], "risk": state["risk"]},
                          default=str, indent=2)
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
