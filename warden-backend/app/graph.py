from functools import partial

from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from .agents import (Deps, approval, audit, critic, extraction, guardrail, ocr, payment_execution, policy_check,
                     risk_scoring)
from .schemas import RunState


def human_review(state: RunState, deps: Deps) -> dict:
    # Durable pause: the run is checkpointed in Postgres and resumes only when
    # someone calls POST /runs/{id}/resume with Command(resume=...).
    answer = interrupt({
        "run_id": state["run_id"],
        "escalation_reasons": state.get("escalation_reasons", []),
        "extraction": state.get("extraction"),
        "risk": state.get("risk"),
        "approval": state.get("approval"),
    })
    approved = answer.get("decision") == "approve"
    update = {
        "human": answer,
        "status": "approved" if approved else "rejected",
        "status_reason": "" if approved else f"rejected by {answer.get('approver', 'reviewer')}",
    }
    details = dict(answer)
    if approved and answer.get("same_vendor_id"):
        # The alias was confirmed by the API before resuming; read the vendor back from
        # the ledger so payment uses the vendor's own name and history, not the invoice's.
        vendor = deps.ledger_for("human-review").lookup_vendor(state["extraction"]["vendor_name"])
        update["vendor"] = vendor
        details["linked_to"] = vendor["vendor_name"]
        details["linked_payment_count"] = vendor["payment_count"]
    update["audit_trail"] = audit("human_review", **details)
    return update


def mark_pending(state: RunState) -> dict:
    # The reasons go into status_reason too, so the Runs list can say why a run is waiting.
    return {"status": "pending_human", "status_reason": "; ".join(state.get("escalation_reasons") or [])}


def start(state: RunState) -> str:
    return "ocr" if state.get("image_base64") else "guardrail"


def after_ocr(state: RunState) -> str:
    return "end" if state.get("status") == "blocked" else "guardrail"


def after_guardrail(state: RunState) -> str:
    return "end" if state["status"] == "blocked" else "extraction"


def after_extraction(state: RunState) -> str:
    return "end" if state["status"] == "blocked" else "risk_scoring"


def after_risk(state: RunState) -> str:
    return "pending_human" if state.get("escalation_reasons") else "approval"


def after_approval(state: RunState) -> str:
    if state.get("status") == "rejected":
        return "end"
    return "pending_human" if state.get("escalation_reasons") else "critic"


def after_critic(state: RunState) -> str:
    return "policy_check" if state["status"] == "approved" else "pending_human"


def after_human(state: RunState) -> str:
    return "policy_check" if state["status"] == "approved" else "end"


def after_policy(state: RunState) -> str:
    # approved: pay. scored: Cedar refused the agents' approval, so a person decides.
    # blocked: Cedar refused a person's approval, so the run stops.
    return {"approved": "payment_execution", "scored": "pending_human"}.get(state["status"], "end")


def after_dry_run(state: RunState) -> str:
    return "settle" if state["status"] == "settling" else "end"


def build_graph(deps: Deps, checkpointer=None):
    g = StateGraph(RunState)
    g.add_node("ocr", partial(ocr.run, deps=deps))
    g.add_node("guardrail", partial(guardrail.run, deps=deps))
    g.add_node("extraction", partial(extraction.run, deps=deps))
    g.add_node("risk_scoring", partial(risk_scoring.run, deps=deps))
    g.add_node("approval", partial(approval.run, deps=deps))
    g.add_node("critic", partial(critic.run, deps=deps))
    g.add_node("pending_human", mark_pending)
    g.add_node("human_review", partial(human_review, deps=deps))
    g.add_node("policy_check", partial(policy_check.run, deps=deps))
    g.add_node("payment_execution", partial(payment_execution.dry_run, deps=deps))
    g.add_node("settle", partial(payment_execution.settle, deps=deps))

    g.set_conditional_entry_point(start, {"ocr": "ocr", "guardrail": "guardrail"})
    g.add_conditional_edges("ocr", after_ocr, {"guardrail": "guardrail", "end": END})
    g.add_conditional_edges("guardrail", after_guardrail, {"extraction": "extraction", "end": END})
    g.add_conditional_edges("extraction", after_extraction, {"risk_scoring": "risk_scoring", "end": END})
    g.add_conditional_edges("risk_scoring", after_risk, {"approval": "approval", "pending_human": "pending_human"})
    g.add_conditional_edges("approval", after_approval,
                            {"critic": "critic", "pending_human": "pending_human", "end": END})
    g.add_conditional_edges("critic", after_critic,
                            {"policy_check": "policy_check", "pending_human": "pending_human"})
    g.add_edge("pending_human", "human_review")
    g.add_conditional_edges("human_review", after_human, {"policy_check": "policy_check", "end": END})
    g.add_conditional_edges("policy_check", after_policy,
                            {"payment_execution": "payment_execution", "pending_human": "pending_human", "end": END})
    g.add_conditional_edges("payment_execution", after_dry_run, {"settle": "settle", "end": END})
    g.add_edge("settle", END)
    return g.compile(checkpointer=checkpointer)
