"""Prompt promotion gate (phase 2, model governance).

Scores each candidate Extraction prompt against held-out SROIE test receipts
with real ground-truth labels, through the exact production code path
(llm.structured + InvoiceExtraction, on the warden-routine route). Every
candidate is registered in the MLflow Prompt Registry with its score; only
the best one that clears PROMOTION_THRESHOLD gets the `production` alias the
backend loads. A candidate below the threshold is registered as BLOCKED and
can never be served.

Passing needs BOTH an overall accuracy of at least PROMOTION_THRESHOLD AND at
least FIELD_FLOOR on every field. The floor was added after the first live
gate run (October 2026): the casual prompt scored 0.85 overall and passed,
while getting the vendor wrong on 16 of 40 receipts (vendor accuracy 0.60).
Date and total are easy for every candidate (0.97), so averaging hid the one
field that decides whether an invoice links to its vendor's payment history.
0.75 was chosen after seeing that run; the reasoning, not the number, is the
rule: no single field may be bad enough to break the pipeline downstream.

    compose run --rm eval-gate

The metric is deterministic, not LLM-judged: three fields against labels.
- vendor: token overlap (Jaccard) >= 0.8 after normalization, so extra words
  such as a cashier's name or "COPY" fail, which is the phase 1 live-run bug
- date: same calendar date across common printed formats
- total: within 0.01
"""
import json
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")
os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
os.environ.setdefault("MLFLOW_HTTP_REQUEST_TIMEOUT", "30")

from deepeval import evaluate  # noqa: E402
from deepeval.evaluate.configs import AsyncConfig, DisplayConfig  # noqa: E402
from deepeval.metrics import BaseMetric  # noqa: E402
from deepeval.test_case import LLMTestCase  # noqa: E402

from . import config, llm  # noqa: E402
from .schemas import InvoiceExtraction  # noqa: E402

PROMPTS = Path(__file__).resolve().parent / "prompts"
CANDIDATES = {
    "structured": PROMPTS / "extraction.txt",
    "casual": PROMPTS / "candidates" / "extraction-casual.txt",
}
THRESHOLD = float(os.environ.get("PROMOTION_THRESHOLD", "0.80"))
FIELD_FLOOR = float(os.environ.get("FIELD_FLOOR", "0.75"))
SAMPLE = int(os.environ.get("EVAL_SAMPLE_SIZE", "40"))
PACE_SECONDS = float(os.environ.get("EVAL_PACE_SECONDS", "1.0"))  # Groq free-tier requests per minute
VENDOR_MIN_OVERLAP = 0.8
DATE_FORMATS = ["%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y", "%Y-%m-%d", "%Y/%m/%d",
                "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%d-%b-%Y", "%d-%b-%y", "%d %b %y"]


# --- scoring ------------------------------------------------------------------------------

def _tokens(name: str | None) -> set[str]:
    return set(re.sub(r"[^A-Z0-9 ]", " ", (name or "").upper()).split())


def vendor_match(predicted: str | None, expected: str | None) -> bool:
    p, e = _tokens(predicted), _tokens(expected)
    return bool(p and e) and len(p & e) / len(p | e) >= VENDOR_MIN_OVERLAP


def parse_date(text: str | None) -> date | None:
    if not text:
        return None
    m = re.search(r"\d{1,4}[/.\-]\d{1,2}[/.\-]\d{2,4}|\d{1,2}[ \-][A-Za-z]{3,9}[ \-,]+\d{2,4}|"
                  r"[A-Za-z]{3,9} \d{1,2}, \d{4}", text)
    candidate = (m.group(0) if m else text).strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(candidate, fmt).date()
        except ValueError:
            continue
    return None


def date_match(predicted: str | None, expected: str | None) -> bool:
    p, e = parse_date(predicted), parse_date(expected)
    if p and e:
        return p == e
    digits = lambda s: re.sub(r"\D", "", s or "")  # noqa: E731
    return bool(digits(expected)) and digits(predicted) == digits(expected)


def to_amount(value) -> float | None:
    if value is None:
        return None
    cleaned = re.sub(r"[^0-9.]", "", str(value))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def total_match(predicted, expected) -> bool:
    p, e = to_amount(predicted), to_amount(expected)
    return p is not None and e is not None and abs(p - e) <= 0.01


def score_fields(predicted: dict, labels: dict) -> dict[str, bool]:
    """Only fields that have a label are scored."""
    checks = {}
    if labels.get("company"):
        checks["vendor"] = vendor_match(predicted.get("vendor_name"), labels["company"])
    if labels.get("date"):
        checks["date"] = date_match(predicted.get("invoice_date"), labels["date"])
    if labels.get("total"):
        checks["total"] = total_match(predicted.get("total_amount"), labels["total"])
    return checks


class FieldAccuracy(BaseMetric):
    """DeepEval metric wrapping score_fields: the share of labelled fields
    extracted correctly for one document. Deterministic; no LLM judge."""

    def __init__(self, threshold: float):
        self.threshold = threshold

    def measure(self, test_case: LLMTestCase) -> float:
        checks = score_fields(json.loads(test_case.actual_output), json.loads(test_case.expected_output))
        self.score = sum(checks.values()) / len(checks) if checks else 0.0
        self.success = self.score >= self.threshold
        self.reason = ", ".join(f"{k}={'ok' if v else 'miss'}" for k, v in checks.items())
        return self.score

    async def a_measure(self, test_case: LLMTestCase) -> float:
        return self.measure(test_case)

    def is_successful(self) -> bool:
        return bool(self.success)

    @property
    def __name__(self):
        return "FieldAccuracy"


# --- running ------------------------------------------------------------------------------

def held_out_receipts(limit: int) -> list[dict]:
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(config.backend_dsn(), row_factory=dict_row) as conn:
        return conn.execute(
            """SELECT external_key, text, ground_truth->'entities' AS labels FROM invoice_inbox
               WHERE source = 'sroie' AND split = 'test' ORDER BY id LIMIT %s""", (limit,)).fetchall()


class ProviderUnavailable(Exception):
    """The provider refused or failed the call (rate limit, quota, outage, timeout).
    That says nothing about the prompt, so the gate stops rather than score it.
    Found in the second live gate run: Groq's free-tier daily token limit made
    calls fail mid-run, and the gate was scoring each refusal as three wrong fields."""


def is_provider_error(exc: BaseException) -> bool:
    import openai
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, (openai.APIConnectionError, openai.APITimeoutError, openai.RateLimitError)):
            return True
        if isinstance(exc, openai.APIStatusError) and (exc.status_code == 429 or exc.status_code >= 500):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def extract(prompt_text: str, document: str) -> tuple[dict, str | None]:
    try:
        out = llm.structured("eval-gate", config.ROUTE_ROUTINE, prompt_text, document, InvoiceExtraction)
        return out.model_dump(), None
    except Exception as exc:
        # Instructor wraps the provider's error; its message keeps the HTTP status.
        if is_provider_error(exc) or re.search(r"Error code: (429|5\d\d)", str(exc)):
            raise ProviderUnavailable(f"{type(exc).__name__}: {str(exc)[:300]}") from exc
        # The model answered but never produced a valid InvoiceExtraction: a real miss.
        return {}, f"{type(exc).__name__}: {str(exc)[:200]}"


def evaluate_candidate(name: str, prompt_text: str, receipts: list[dict]) -> dict:
    cases, rows = [], []
    for i, r in enumerate(receipts, 1):
        predicted, error = extract(prompt_text, r["text"])
        cases.append(LLMTestCase(input=r["external_key"], actual_output=json.dumps(predicted),
                                 expected_output=json.dumps(r["labels"])))
        rows.append({"key": r["external_key"], "predicted": predicted, "labels": r["labels"],
                     "checks": score_fields(predicted, r["labels"]), "error": error})
        print(f"  [{name}] {i}/{len(receipts)} {r['external_key']} {rows[-1]['checks']}"
              + (f" ERROR {error}" if error else ""), flush=True)
        time.sleep(PACE_SECONDS)

    result = evaluate(cases, [FieldAccuracy(THRESHOLD)],
                      display_config=DisplayConfig(print_results=False, show_indicator=False),
                      async_config=AsyncConfig(run_async=False))
    doc_scores = [t.metrics_data[0].score for t in result.test_results]
    per_field = {}
    for field in ("vendor", "date", "total"):
        scored = [row["checks"][field] for row in rows if field in row["checks"]]
        per_field[field] = sum(scored) / len(scored) if scored else 0.0
    return {"name": name, "accuracy": sum(doc_scores) / len(doc_scores), "per_field": per_field,
            "errors": sum(1 for row in rows if row["error"]), "rows": rows}


def blocking_reasons(result: dict) -> list[str]:
    reasons = []
    if result["accuracy"] < THRESHOLD:
        reasons.append(f"accuracy {result['accuracy']:.3f} < {THRESHOLD:.2f}")
    reasons += [f"{field} {value:.2f} < floor {FIELD_FLOOR:.2f}"
                for field, value in result["per_field"].items() if value < FIELD_FLOOR]
    return reasons


def register(result: dict, prompt_text: str, promoted: bool) -> int:
    import mlflow
    mlflow.set_experiment("warden-prompt-gate")
    blocked = blocking_reasons(result)
    verdict = "PROMOTED" if promoted else ("BLOCKED" if blocked else "PASSED")
    with mlflow.start_run(run_name=f"extraction-{result['name']}") as run:
        mlflow.log_params({"candidate": result["name"], "threshold": THRESHOLD, "field_floor": FIELD_FLOOR,
                           "sample_size": len(result["rows"]),
                           "route": config.ROUTE_ROUTINE})
        mlflow.log_metrics({"accuracy": result["accuracy"], "errors": result["errors"],
                            **{f"{k}_accuracy": v for k, v in result["per_field"].items()}})
        mlflow.log_dict({"rows": result["rows"]}, "per_document.json")
        pv = mlflow.genai.register_prompt(
            name=config.EXTRACTION_PROMPT_NAME, template=prompt_text,
            commit_message=f"gate run {run.info.run_id}: accuracy {result['accuracy']:.3f} ({verdict})",
            tags={"candidate": result["name"], "gate": verdict, "accuracy": f"{result['accuracy']:.3f}",
                  "threshold": str(THRESHOLD), "field_floor": str(FIELD_FLOOR),
                  "blocked_because": "; ".join(blocked) or "-"})
    if promoted:
        mlflow.genai.set_prompt_alias(config.EXTRACTION_PROMPT_NAME, "production", pv.version)
    return pv.version


def main() -> int:
    import mlflow
    mlflow.set_tracking_uri(config.MLFLOW_TRACKING_URI)
    receipts = held_out_receipts(SAMPLE)
    if not receipts:
        print("no SROIE test receipts in invoice_inbox; run the data loader first")
        return 2
    print(f"Promotion gate: {len(receipts)} held-out SROIE receipts, overall threshold {THRESHOLD:.2f}, "
          f"per-field floor {FIELD_FLOOR:.2f}, route {config.ROUTE_ROUTINE}")

    texts = {name: path.read_text() for name, path in CANDIDATES.items()}
    try:
        results = [evaluate_candidate(name, texts[name], receipts) for name in CANDIDATES]
    except ProviderUnavailable as exc:
        print(f"\nINCONCLUSIVE: the provider failed a call, so the scores would not measure the prompts.\n"
              f"  {exc}\nNothing was registered and the `production` alias was not changed. "
              f"Fix the cause (for example wait for a daily token limit to reset) and run the gate again.")
        return 3
    passing = [r for r in results if not blocking_reasons(r)]
    winner = max(passing, key=lambda r: r["accuracy"]) if passing else None

    print("\ncandidate     accuracy  vendor  date   total  errors  verdict      registry version     why")
    for r in results:
        promoted = r is winner
        version = register(r, texts[r["name"]], promoted)
        verdict = "PROMOTED" if promoted else ("passed" if r in passing else "BLOCKED")
        f = r["per_field"]
        print(f"{r['name']:<13} {r['accuracy']:>8.3f}  {f['vendor']:>6.2f}  {f['date']:>5.2f}  {f['total']:>5.2f}"
              f"  {r['errors']:>6}  {verdict:<11}  {config.EXTRACTION_PROMPT_NAME} v{version:<4}"
              f"  {'; '.join(blocking_reasons(r)) or '-'}")

    if not winner:
        print("\nNo candidate passed; the `production` alias was not changed.")
        return 1
    print(f"\n`production` -> {winner['name']}. Restart warden-backend to load it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
