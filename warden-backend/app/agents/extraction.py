from .. import config
from ..schemas import InvoiceExtraction, RunState
from . import Deps, audit, prompt


def run(state: RunState, deps: Deps) -> dict:
    ext = deps.llm("extraction", config.ROUTE_ROUTINE, prompt("extraction"), state["invoice_text"], InvoiceExtraction)

    problems = []
    if not ext.vendor_name:
        problems.append("vendor name not found")
    if ext.total_amount is None or ext.total_amount <= 0:
        problems.append("total amount not found")
    elif ext.line_items:
        items_total = sum(i.amount for i in ext.line_items)
        if abs(items_total - ext.total_amount) > config.LINE_ITEM_TOLERANCE * ext.total_amount:
            problems.append(f"line items sum to {items_total:.2f} but total is {ext.total_amount:.2f}")

    update = {"extraction": ext.model_dump()}
    if problems:
        reason = "additional documentation requested: " + "; ".join(problems)
        return update | {"status": "blocked", "status_reason": reason,
                         "audit_trail": audit("extraction", outcome="needs_documentation", problems=problems)}
    return update | {"status": "extracted",
                     "audit_trail": audit("extraction", vendor=ext.vendor_name, total=ext.total_amount,
                                          confidence=ext.extraction_confidence)}
