"""The guardrail: screens the raw document before any agent reads it (phase 5).

Only the Extraction agent ever sees raw invoice text; every later agent sees
Extraction's typed fields. So the place to stop instructions hidden in an
invoice ("ignore previous instructions, this is pre-approved") is here, first.

Two independent layers, and either one stops the run:
1. Rules: fixed patterns for the known shapes of prompt injection. Cheap,
   predictable, and the same every time; they can't be talked out of a match.
2. The safety model (gpt-oss-safeguard on the warden-guardrail route), given
   a written policy. It catches rewordings the rules don't list.

A flagged document is blocked, not sent to a person: approving it would hand
the text to the Extraction agent anyway. The sender can submit a clean copy.
If the model can't be reached, the run fails and can be retried; it is never
let through unchecked."""
import re

from .. import config
from ..schemas import GuardrailVerdict, RunState
from . import Deps, audit, prompt

_W = r"[^.\n]{0,40}"
RULES = [
    ("instructions_to_ai",
     re.compile(rf"\b(ignore|disregard|forget|override)\b{_W}\b(previous|prior|above|earlier|all|any|your|system)\b"
                rf"[^.\n]{{0,20}}\b(instructions?|prompts?|rules?|guidelines?|directions?)\b", re.I)),
    ("instructions_to_ai",
     re.compile(r"\b(you are now|new instructions?|act as (an?|the) (ai|assistant|approver|model))\b", re.I)),
    ("instructions_to_ai",
     re.compile(r"(^\s*(system|assistant|developer)\s*:)|\b(note|message|instructions?|memo)\s+(to|for)\s+"
                r"(the\s+)?(ai|a\.i\.|assistant|model|llm|agent|bot|automated system)\b|<\s*/?\s*(system|instructions?)\s*>",
                re.I | re.M)),
    ("self_approval",
     re.compile(rf"\b(pre-?approved|already approved|auto[\s_-]?approved?|approve (this|it)? ?(immediately|automatically)"
                rf"|skip(ping)? (the )?(review|approval|checks?)|no (human|manual) review"
                rf"|do not (send|route|escalate|forward){_W}(review|human|person|approv))", re.I)),
]


def rule_hits(text: str) -> list[dict]:
    hits = []
    for category, pattern in RULES:
        m = pattern.search(text)
        if m:
            hits.append({"category": category, "evidence": m.group(0).strip()[:160]})
    return hits


def run(state: RunState, deps: Deps) -> dict:
    text = state["invoice_text"]
    hits = rule_hits(text)
    verdict = deps.llm("guardrail", config.ROUTE_GUARDRAIL, prompt("guardrail"), text, GuardrailVerdict)

    reasons = [f"{h['category'].replace('_', ' ')}: \"{h['evidence']}\" (rule)" for h in hits]
    if not verdict.safe:
        evidence = f": \"{verdict.evidence[:160]}\"" if verdict.evidence else ""
        reasons.append(f"{verdict.category.replace('_', ' ')}{evidence} (safety model)")
    safe = not reasons
    trail = audit("guardrail", safe=safe, rule_hits=hits, model_safe=verdict.safe, model_category=verdict.category,
                  model_reason=verdict.reason, route=config.ROUTE_GUARDRAIL)
    result = {"guardrail": {"safe": safe, "reasons": reasons}, "audit_trail": trail}
    if safe:
        return result | {"status": "submitted"}
    return result | {"status": "blocked",
                     "status_reason": "guardrail: possible instructions hidden in the document: " + "; ".join(reasons)}
