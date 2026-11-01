from .. import config
from ..schemas import InvoiceExtraction, RunState
from . import Deps, audit

# Common printed forms the model may return instead of an ISO code.
CURRENCY_ALIASES = {"RM": "MYR", "$": "USD", "US$": "USD", "RP": "IDR", "S$": "SGD"}


def normalize_currency(value: str | None) -> tuple[str, bool]:
    """Returns (currency, assumed). A document that shows none is taken to be in
    the ledger currency, which is the business rule, recorded as assumed."""
    if not value:
        return config.LEDGER_CURRENCY, True
    code = value.strip().upper()
    return CURRENCY_ALIASES.get(code, code), False


def run(state: RunState, deps: Deps) -> dict:
    loaded = deps.extraction_prompt
    ext = deps.llm("extraction", config.ROUTE_ROUTINE, loaded.text, state["invoice_text"], InvoiceExtraction)

    problems = []
    if not ext.vendor_name:
        problems.append("vendor name not found")
    if ext.total_amount is None or ext.total_amount <= 0:
        problems.append("total amount not found")
    elif ext.line_items:
        items_total = sum(i.amount for i in ext.line_items)
        if abs(items_total - ext.total_amount) > config.LINE_ITEM_TOLERANCE * ext.total_amount:
            problems.append(f"line items sum to {items_total:.2f} but total is {ext.total_amount:.2f}")

    currency, assumed = normalize_currency(ext.currency)
    extraction = ext.model_dump() | {"currency": currency, "currency_assumed": assumed}
    prompt_info = {"prompt_source": loaded.source, "prompt_version": loaded.version}
    if problems:
        reason = "additional documentation requested: " + "; ".join(problems)
        return {"extraction": extraction, "status": "blocked", "status_reason": reason,
                "audit_trail": audit("extraction", outcome="needs_documentation", problems=problems, **prompt_info)}
    return {"extraction": extraction, "status": "extracted",
            "audit_trail": audit("extraction", vendor=ext.vendor_name, total=ext.total_amount, currency=currency,
                                 currency_assumed=assumed, confidence=ext.extraction_confidence, **prompt_info)}
