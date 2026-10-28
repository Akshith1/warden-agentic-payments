from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
import time

from .. import llm
from ..ledger_client import LedgerClient

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"


def prompt(name: str) -> str:
    # Phase 2 replaces this with the MLflow `production` alias, keeping these files as the fallback.
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
