from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
import time

from .. import llm
from ..prompt_registry import LoadedPrompt, bundled
from ..ledger_client import LedgerClient

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"


def prompt(name: str) -> str:
    # Risk, Approval and Critic prompts are versioned in git; Extraction's comes from
    # the MLflow registry (see prompt_registry) because it is the one the eval gate scores.
    return (PROMPTS / f"{name}.txt").read_text()


def audit(step: str, **details) -> list[dict]:
    return [{"step": step, "at": datetime.now(timezone.utc).isoformat(), **details}]


@dataclass
class Deps:
    """What the nodes need from the outside world, passed in so tests can
    supply their own doubles without touching the graph."""
    llm: Callable = llm.structured
    ledger: LedgerClient = field(default_factory=LedgerClient)
    sleep: Callable[[float], None] = time.sleep
    extraction_prompt: LoadedPrompt = field(default_factory=bundled)
