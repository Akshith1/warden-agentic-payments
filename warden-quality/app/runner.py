"""Runs every check with Great Expectations and reports the result.

Each run reads fresh batches through a read-only Postgres role, so a check sees
the data as it is now. The latest result stays in memory for the console. It
goes to the control plane's audit log whenever something failed or the outcome
changed, and a critical failure there trips the kill switch."""
import logging
import threading
import time
from datetime import datetime, timezone

import great_expectations as gx
import httpx

from . import config
from .checks import CHECKS, CRITICAL, Check

log = logging.getLogger("warden.quality")
_lock = threading.Lock()
_latest: dict | None = None


def _context():
    ctx = gx.get_context(mode="ephemeral")
    ctx.variables.progress_bars = {"globally": False}
    sources = {"ledger": ctx.data_sources.add_postgres(name="ledger", connection_string=config.dsn(config.LEDGER_DB)),
               "backend": ctx.data_sources.add_postgres(name="backend",
                                                        connection_string=config.dsn(config.BACKEND_DB))}
    return ctx, sources


class CheckError(Exception):
    pass


def _outcome(check: Check, result) -> dict:
    info = result.exception_info or {}
    if info.get("raised_exception"):
        raise CheckError(info.get("exception_message") or "the check could not run")
    r = result.result or {}
    if "observed_value" in r and "details" in r:      # a query listing the rows that break the rule
        count = int(r["observed_value"] or 0)
        examples = [next(iter(row.values())) for row in r["details"].get("unexpected_rows", [])[:5]]
    elif "unexpected_count" in r:                      # a column rule
        count = int(r["unexpected_count"] or 0)
        examples = r.get("partial_unexpected_list", [])[:5]
    else:                                              # a table rule, e.g. row count
        count = 0 if result.success else 1
        examples = [f"{r.get('observed_value')} rows"] if "observed_value" in r else []
    out = {"status": "passed" if result.success else "failed", "count": 0 if result.success else max(count, 1)}
    if not result.success:
        # Ids are always safe to show; values only where the check says they aren't personal.
        is_id_query = "details" in r
        out["examples"] = [str(e) for e in examples] if (is_id_query or check.show_values) else []
    return out


def run_checks(requested_by: str = "schedule") -> dict:
    with _lock:
        started = time.monotonic()
        ctx, sources = _context()
        batches: dict[tuple[str, str], object] = {}
        results = []
        for check in CHECKS:
            entry = {"id": check.id, "suite": check.suite, "severity": check.severity, "title": check.title,
                     "why": check.why}
            try:
                key = (check.database, check.table)
                if key not in batches:
                    asset = sources[check.database].add_table_asset(name=f"{check.database}_{check.table}",
                                                                    table_name=check.table)
                    batches[key] = asset.add_batch_definition_whole_table("whole table").get_batch()
                result = batches[key].validate(check.expectation(), result_format="COMPLETE")
                entry |= _outcome(check, result)
            except Exception as e:  # noqa: BLE001 - one unreadable table must not hide the other results
                entry |= {"status": "error", "count": 0, "error": _short(e)}
            results.append(entry)
        summary = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "requested_by": requested_by,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "great_expectations": gx.__version__,
            "passed": sum(r["status"] == "passed" for r in results),
            "failed": sum(r["status"] == "failed" for r in results),
            "errors": sum(r["status"] == "error" for r in results),
            "critical_failed": [r["id"] for r in results if r["status"] == "failed" and r["severity"] == CRITICAL],
            "checks": results,
        }
        _record(summary)
        return summary


def latest() -> dict | None:
    return _latest


def _signature(s: dict | None):
    return None if s is None else sorted((r["id"], r["status"], r["count"]) for r in s["checks"])


def _record(summary: dict) -> None:
    """Keep the result; tell the control plane when it matters. A run that finds anything
    critical is always sent, so the kill switch trips again if someone resets it while the
    data is still wrong. A clean run is sent only when it changes the picture, and any run a
    person asked for is sent, so the log shows who checked and what they saw."""
    global _latest
    previous, _latest = _latest, summary
    if not (summary["critical_failed"] or _signature(previous) != _signature(summary)
            or summary["requested_by"] not in ("schedule", "service")):
        return
    failed = [{"id": r["id"], "severity": r["severity"], "title": r["title"], "count": r["count"],
               "examples": r.get("examples", [])} for r in summary["checks"] if r["status"] == "failed"]
    errors = [{"id": r["id"], "error": r["error"]} for r in summary["checks"] if r["status"] == "error"]
    payload = {k: summary[k] for k in ("passed", "failed", "errors", "critical_failed", "duration_ms")}
    payload |= {"failed_checks": failed, "error_checks": errors}
    try:
        httpx.post(f"{config.CONTROL_URL}/events", timeout=5,
                   json={"type": "data_quality", "actor": summary["requested_by"], "payload": payload})
    except httpx.HTTPError as e:
        log.warning("could not record the data-quality result in the audit log: %s", e)


def _short(e: Exception) -> str:
    text = str(getattr(e, "orig", None) or e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
    return text[:240]
