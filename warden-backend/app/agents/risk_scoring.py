import json
import re

from .. import config
from ..schemas import RiskAssessment, RunState
from . import Deps, audit, prompt


def _account_key(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def run(state: RunState, deps: Deps) -> dict:
    ext = state["extraction"]
    # Vendor facts come from the ledger, never from the invoice or the model:
    # a self-reported "known vendor" flag would let anyone skip the first-seen rule.
    vendor = deps.ledger.lookup_vendor(ext["vendor_name"])

    route = config.ROUTE_REASONING if ext["total_amount"] > config.ROUTINE_AMOUNT_THRESHOLD else config.ROUTE_ROUTINE
    evidence = json.dumps({"invoice": ext, "vendor_history": vendor}, default=str, indent=2)
    risk = deps.llm("risk-scoring", route, prompt("risk_scoring"), evidence, RiskAssessment)

    reasons = []
    if not vendor["known"]:
        reasons.append("first-seen vendor")
    if risk.risk_level == "high":
        reasons.append(f"high risk score {risk.risk_score:.2f}")
    last, new = _account_key(vendor.get("last_remit_account")), _account_key(ext.get("remit_to_account"))
    if last and new and last != new:
        reasons.append("remit-to account differs from the vendor's last settled payment")

    return {
        "vendor": vendor,
        "risk": risk.model_dump(),
        "escalation_reasons": reasons,
        "status": "scored",
        "audit_trail": audit("risk_scoring", route=route, risk_level=risk.risk_level,
                             risk_score=risk.risk_score, vendor_known=vendor["known"], escalation=reasons),
    }
