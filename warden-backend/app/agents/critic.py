import json

from .. import config
from ..schemas import CriticReview, RunState
from . import Deps, audit, prompt
from .approval import rules_context


def run(state: RunState, deps: Deps) -> dict:
    # Approval runs on warden-reasoning (OpenAI); the critic deliberately uses
    # warden-routine (Groq) so the second opinion comes from a different provider.
    evidence = json.dumps({"invoice": state["extraction"], "vendor_history": state["vendor"],
                           "risk": state["risk"], "decision_under_review": state["approval"],
                           "rules_already_enforced": rules_context()},
                          default=str, indent=2)
    review = deps.llm("critic", config.ROUTE_ROUTINE, prompt("critic"), evidence, CriticReview)

    update = {"critic": review.model_dump(), "audit_trail": audit("critic", agrees=review.agrees,
                                                                    concerns=review.concerns)}
    if not review.agrees:
        update["escalation_reasons"] = ["critic disagreed: " + "; ".join(review.concerns)]
    else:
        update["status"] = "approved"
    return update
