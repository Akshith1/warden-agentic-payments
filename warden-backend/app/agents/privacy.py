"""Masks personal data before any model reads the document (phase 7).

Runs after OCR and before the guardrail. The masked text is what the
guardrail and the Extraction agent see; the mapping stays in the run's state
and is never sent to a model. The trail records which kinds of personal data
were found and how many, never the values."""
from ..schemas import RunState
from . import Deps, audit


def run(state: RunState, deps: Deps) -> dict:
    out = deps.pii.mask(state["invoice_text"])
    return {"masked_text": out["masked"], "pii_mapping": out["mapping"],
            "audit_trail": audit("privacy", masked=out["counts"], total=sum(out["counts"].values()))}
