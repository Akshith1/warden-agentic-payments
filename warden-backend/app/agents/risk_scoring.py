import json
import re

from .. import config
from ..privacy import shield
from ..schemas import RiskAssessment, RunState
from . import Deps, audit, prompt


def _account_key(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def remit_changed(vendor: dict, ext: dict) -> bool:
    last, new = _account_key(vendor.get("last_remit_account")), _account_key(ext.get("remit_to_account"))
    return bool(last and new and last != new)


def assess(deps: Deps, ext: dict, vendor: dict, pii_mapping: dict) -> tuple[str, RiskAssessment]:
    """The model's part of risk scoring, on its own so the consistency check (phase 9b) can ask
    the same question again with exactly the evidence a run sent."""
    route = config.ROUTE_REASONING if ext["total_amount"] > config.ROUTINE_AMOUNT_THRESHOLD else config.ROUTE_ROUTINE
    # Personal data goes to the model as the placeholders the PII step chose (phase 7).
    evidence = json.dumps(shield({"invoice": ext, "vendor_history": vendor}, pii_mapping), default=str, indent=2)
    return route, deps.llm("risk-scoring", route, prompt("risk_scoring"), evidence, RiskAssessment)


def run(state: RunState, deps: Deps) -> dict:
    ext = state["extraction"]
    # Vendor facts come from the ledger, never from the invoice or the model:
    # a self-reported "known vendor" flag would let anyone skip the first-seen rule.
    vendor = deps.ledger_for("risk-scoring").lookup_vendor(ext["vendor_name"])

    route, risk = assess(deps, ext, vendor, state.get("pii_mapping") or {})

    reasons = []
    if not vendor["known"]:
        reasons.append("first-seen vendor")
    elif vendor["payment_count"] < config.MIN_PRIOR_PAYMENTS:
        reasons.append(f"limited history: {vendor['payment_count']} prior payment(s), "
                       f"auto-approval needs {config.MIN_PRIOR_PAYMENTS}")
    if ext["currency"] != config.LEDGER_CURRENCY:
        reasons.append(f"currency {ext['currency']} differs from ledger currency {config.LEDGER_CURRENCY}")
    if risk.risk_level == "high":
        reasons.append(f"high risk score {risk.risk_score:.2f}")
    if remit_changed(vendor, ext):
        reasons.append("remit-to account differs from the vendor's last settled payment")

    return {
        "vendor": vendor,
        "risk": risk.model_dump(),
        "escalation_reasons": reasons,
        "status": "scored",
        "audit_trail": audit("risk_scoring", route=route, risk_level=risk.risk_level,
                             risk_score=risk.risk_score, vendor_known=vendor["known"], escalation=reasons),
    }
