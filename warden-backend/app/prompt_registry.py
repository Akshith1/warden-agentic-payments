"""Loads the Extraction prompt from the MLflow Prompt Registry's `production`
alias, the version the eval gate promoted. Falls back to the bundled file if
MLflow is not configured, unreachable, or has nothing promoted yet; the
fallback is logged and shown on /health, never silent. Loaded once at
process start: a newly promoted prompt takes effect on restart."""
import logging
import os
from dataclasses import dataclass
from pathlib import Path

# MLflow reads these once, at import; set them first or an unreachable server
# turns startup into a multi-minute retry loop instead of a fast fallback.
os.environ.setdefault("MLFLOW_HTTP_REQUEST_TIMEOUT", "5")
os.environ.setdefault("MLFLOW_HTTP_REQUEST_MAX_RETRIES", "1")

from . import config  # noqa: E402

log = logging.getLogger("warden.prompts")
BUNDLED = Path(__file__).resolve().parent / "prompts" / "extraction.txt"


@dataclass(frozen=True)
class LoadedPrompt:
    text: str
    source: str  # "mlflow" or "bundled"
    version: str | None = None


def bundled() -> LoadedPrompt:
    return LoadedPrompt(BUNDLED.read_text(), "bundled")


def load_extraction_prompt() -> LoadedPrompt:
    if not config.MLFLOW_TRACKING_URI:
        return bundled()
    try:
        import mlflow
        mlflow.set_tracking_uri(config.MLFLOW_TRACKING_URI)
        pv = mlflow.genai.load_prompt(f"prompts:/{config.EXTRACTION_PROMPT_NAME}@production")
        log.info("extraction prompt: %s version %s from MLflow", config.EXTRACTION_PROMPT_NAME, pv.version)
        return LoadedPrompt(pv.template, "mlflow", str(pv.version))
    except Exception as exc:
        log.warning("extraction prompt: using bundled file (%s: %s)", type(exc).__name__, exc)
        return bundled()
