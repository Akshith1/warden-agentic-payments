import json

from .. import config
from ..privacy import shield
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
        "routine_amount_limit": config.ROUTINE_AMOUNT_THRESHOLD,
        "amount_rule": "Invoices above the routine amount limit go to a person whatever you decide, by code and "
                       "by policy; don't escalate for that reason alone.",
    }


def decide(deps: Deps, ext: dict, vendor: dict, risk: dict, pii_mapping: dict) -> ApprovalDecision:
    """The model's part of approval, on its own for the consistency check (phase 9b)."""
    evidence = json.dumps(shield({"invoice": ext, "vendor_history": vendor, "risk": risk,
                                  "rules_already_enforced": rules_context()}, pii_mapping), default=str, indent=2)
    return deps.llm("approval", config.ROUTE_REASONING, prompt("approval"), evidence, ApprovalDecision)


def approve(deps: Deps, ext: dict, vendor: dict, risk: dict, pii_mapping: dict) -> tuple[ApprovalDecision,
                                                                                     list[ApprovalDecision]]:
    """The approval agent's answer as Warden acts on it (phase 9b). Escalate and reject stand on the
    first answer. An auto_approve stands only if every one of APPROVAL_SAMPLES asks says so; any
    other answer sends the invoice to a person, and the lowest confidence is the one checked."""
    first = decide(deps, ext, vendor, risk, pii_mapping)
    samples = [first]
    if first.decision != "auto_approve":
        return first, samples
    while len(samples) < config.APPROVAL_SAMPLES and samples[-1].decision == "auto_approve":
        samples.append(decide(deps, ext, vendor, risk, pii_mapping))
    lowest = min(s.confidence for s in samples)
    agreed = sum(s.decision == "auto_approve" for s in samples)
    if agreed == len(samples):
        return ApprovalDecision(decision="auto_approve", confidence=lowest, reasons=first.reasons), samples
    dissent = next(s for s in samples if s.decision != "auto_approve")
    return ApprovalDecision(decision="escalate", confidence=lowest, reasons=[
        f"not unanimous: {agreed} of {len(samples)} asks said auto_approve, then one said {dissent.decision}",
        *dissent.reasons]), samples


def run(state: RunState, deps: Deps) -> dict:
    ext = state["extraction"]
    decision, samples = approve(deps, ext, state["vendor"], state["risk"], state.get("pii_mapping") or {})

    trail = audit("approval", decision=decision.decision, confidence=decision.confidence, asks=len(samples),
                  answers=[s.decision for s in samples])
    if decision.decision == "reject":
        return {"approval": decision.model_dump(), "status": "rejected",
                "status_reason": "; ".join(decision.reasons), "audit_trail": trail}

    # Code-level guards. The policy node then checks the same rules again in Cedar (phase 4).
    reasons = list(state.get("escalation_reasons", []))
    if ext["total_amount"] > config.ROUTINE_AMOUNT_THRESHOLD:
        reasons.append(f"amount above ${config.ROUTINE_AMOUNT_THRESHOLD:,.0f} needs a human approver")
    if decision.confidence < config.CONFIDENCE_THRESHOLD:
        reasons.append(f"decision confidence {decision.confidence:.2f} below {config.CONFIDENCE_THRESHOLD}")
    if decision.decision == "escalate":
        reasons.append("approval agent escalated: " + "; ".join(decision.reasons))

    return {"approval": decision.model_dump(), "escalation_reasons": reasons, "audit_trail": trail}
