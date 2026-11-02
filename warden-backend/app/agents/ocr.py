"""Reads a scanned invoice through the sandboxed OCR service (phase 5). The
image never touches this process's filesystem; the text goes to the guardrail
next, like pasted text."""
from ..capability import require_capability
from ..ocr_client import Unreadable
from ..schemas import RunState
from . import Deps, audit


def run(state: RunState, deps: Deps) -> dict:
    require_capability("extraction", "RunSandboxedOcr")  # the extraction role owns reading documents
    try:
        result = deps.ocr.read(state["image_base64"])
    except Unreadable as e:
        return {"status": "blocked", "status_reason": f"the image could not be read: {e}", "image_base64": None,
                "audit_trail": audit("ocr", ok=False, error=str(e))}
    text = result["text"]
    trail = audit("ocr", ok=bool(text), chars=result["chars"], seconds=result["seconds"],
                  image_type=result["image_type"], landlock_abi=result["sandbox"]["landlock_abi"])
    if not text:
        return {"status": "blocked", "status_reason": "no text found in the image", "image_base64": None,
                "audit_trail": trail}
    return {"invoice_text": text, "image_base64": None, "audit_trail": trail}
