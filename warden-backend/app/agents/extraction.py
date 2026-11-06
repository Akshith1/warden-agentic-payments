from .. import config
from ..schemas import InvoiceExtraction, RunState
from ..privacy import restore
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
    mapping = state.get("pii_mapping") or {}
    # The model reads the masked text; the real values go back into its answer here, in code.
    ext = deps.llm("extraction", config.ROUTE_ROUTINE, loaded.text, state.get("masked_text") or state["invoice_text"],
                   InvoiceExtraction)
    ext = InvoiceExtraction.model_validate(restore(ext.model_dump(), mapping))

    problems = []
    if not ext.vendor_name:
        problems.append("vendor name not found")
    if ext.total_amount is None or ext.total_amount <= 0:
        problems.append("total amount not found")
    elif ext.line_items:
        # Items, plus tax, plus rounding, service charge or discount, must come to the total.
        # Phase 11 live run: an invoice with 6% SST was stopped, because only the items were added.
        items_total = sum(i.amount for i in ext.line_items)
        expected = items_total + (ext.tax_amount or 0) + (ext.adjustments or 0)
        if abs(expected - ext.total_amount) > config.LINE_ITEM_TOLERANCE * ext.total_amount:
            parts = [f"line items sum to {items_total:.2f}"]
            if ext.tax_amount:
                parts.append(f"tax {ext.tax_amount:.2f}")
            if ext.adjustments:
                parts.append(f"adjustments {ext.adjustments:+.2f}")
            problems.append(f"{' plus '.join(parts)} but total is {ext.total_amount:.2f}")

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
