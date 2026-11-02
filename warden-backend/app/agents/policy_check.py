"""The Cedar check before money moves (phase 4).

Runs after the agents decide to auto-approve, and after a person approves. The
code rules in risk scoring and approval are the first check; this is the second,
independent one: different code, different service, rules written as policy.
When it says no to an agent, the run goes to a person with the policy's reasons.
When it says no to a person, the run stops."""
from ..control_client import KillSwitchOn
from ..schemas import RunState
from . import Deps, audit
from .risk_scoring import remit_changed


def payment_facts(state: RunState) -> dict:
    """What the run knows about the payment, for the policy service. Vendor history is
    not included: the policy service reads that from the ledger itself."""
    ext = state["extraction"]
    return {
        "amount": f"{ext['total_amount']:.2f}",
        "currency": ext.get("currency") or "",
        "vendor": ext["vendor_name"],
        "confidence": (state.get("approval") or {}).get("confidence", 0),
        "critic_agrees": bool((state.get("critic") or {}).get("agrees")),
        "remit_changed": remit_changed(state.get("vendor") or {}, ext),
        "submitted_by": state.get("submitted_by") or "",
    }


def run(state: RunState, deps: Deps) -> dict:
    human = state.get("human") or {}
    by_person = human.get("decision") == "approve"
    principal = ("User", human.get("approver") or "reviewer") if by_person else ("Agent", "approval")
    # A person's roles come from their verified login, recorded by the API when they decided.
    roles = human.get("roles") or [] if by_person else []
    result = deps.policy.authorize(principal, "ApprovePayment", payment=payment_facts(state), roles=roles)
    if result.get("kill_switch"):
        raise KillSwitchOn(result["reasons"][0])
    allowed = result["decision"] == "Allow"
    facts = result.get("facts", {})
    trail = audit("policy", principal=f"{principal[0]}:{principal[1]}", action="ApprovePayment",
                  decision=result["decision"], rules=result["policies"], reasons=result["reasons"],
                  prior_payments=facts.get("priorPayments"), payments_last_24h=facts.get("paymentsLast24h"),
                  policy_set=result.get("policy_set"))
    if allowed:
        return {"status": "approved", "policy": result, "audit_trail": trail}
    if by_person:
        return {"status": "blocked", "status_reason": "policy refused: " + "; ".join(result["reasons"]),
                "policy": result, "audit_trail": trail}
    return {"status": "scored", "policy": result,
            "escalation_reasons": [f"policy: {r}" for r in result["reasons"]], "audit_trail": trail}
