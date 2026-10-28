from functools import partial

from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from .agents import Deps, approval, audit, critic, extraction, payment_execution, risk_scoring
from .schemas import RunState


def human_review(state: RunState) -> dict:
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
    return {
        "human": answer,
        "status": "approved" if approved else "rejected",
        "status_reason": "" if approved else f"rejected by {answer.get('approver', 'reviewer')}",
        "audit_trail": audit("human_review", **answer),
    }


def mark_pending(_state: RunState) -> dict:
    return {"status": "pending_human"}


def after_extraction(state: RunState) -> str:
    return "end" if state["status"] == "blocked" else "risk_scoring"


def after_risk(state: RunState) -> str:
    return "pending_human" if state.get("escalation_reasons") else "approval"


def after_approval(state: RunState) -> str:
    if state.get("status") == "rejected":
        return "end"
    return "pending_human" if state.get("escalation_reasons") else "critic"


def after_critic(state: RunState) -> str:
    return "payment_execution" if state["status"] == "approved" else "pending_human"


def after_human(state: RunState) -> str:
    return "payment_execution" if state["status"] == "approved" else "end"


def build_graph(deps: Deps, checkpointer=None):
    g = StateGraph(RunState)
    g.add_node("extraction", partial(extraction.run, deps=deps))
    g.add_node("risk_scoring", partial(risk_scoring.run, deps=deps))
    g.add_node("approval", partial(approval.run, deps=deps))
    g.add_node("critic", partial(critic.run, deps=deps))
    g.add_node("pending_human", mark_pending)
    g.add_node("human_review", human_review)
    g.add_node("payment_execution", partial(payment_execution.run, deps=deps))

    g.set_entry_point("extraction")
    g.add_conditional_edges("extraction", after_extraction, {"risk_scoring": "risk_scoring", "end": END})
    g.add_conditional_edges("risk_scoring", after_risk, {"approval": "approval", "pending_human": "pending_human"})
    g.add_conditional_edges("approval", after_approval,
                            {"critic": "critic", "pending_human": "pending_human", "end": END})
    g.add_conditional_edges("critic", after_critic,
                            {"payment_execution": "payment_execution", "pending_human": "pending_human"})
    g.add_edge("pending_human", "human_review")
    g.add_conditional_edges("human_review", after_human, {"payment_execution": "payment_execution", "end": END})
    g.add_edge("payment_execution", END)
    return g.compile(checkpointer=checkpointer)
