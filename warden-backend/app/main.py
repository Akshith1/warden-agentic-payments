import json
import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from . import config, entry, telemetry, wake
from .agents import Deps
from .agents.policy_check import payment_facts
from .auth import User, require
from .control_client import KillSwitchOn
from .graph import build_graph
from .prompt_registry import load_extraction_prompt

log = logging.getLogger("warden")

RUNS_TABLE = """
CREATE TABLE IF NOT EXISTS runs (
    run_id         TEXT PRIMARY KEY,
    status         TEXT NOT NULL,
    status_reason  TEXT,
    vendor_name    TEXT,
    total_amount   NUMERIC(18,2),
    source         TEXT NOT NULL,
    submitted_by   TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class Runtime:
    pool: ConnectionPool
    graph = None
    deps: Deps
    workers: ThreadPoolExecutor


rt = Runtime()


@asynccontextmanager
async def lifespan(_app):
    # The checkpointer needs autocommit + dict rows; a pool keeps worker threads off one shared connection.
    rt.pool = ConnectionPool(config.backend_dsn(), max_size=10, open=True,
                             kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})
    rt.pool.wait(timeout=float(os.environ.get("DB_WAIT_SECONDS", "60")))  # Postgres may still be starting
    checkpointer = PostgresSaver(rt.pool)
    checkpointer.setup()
    with rt.pool.connection() as conn:
        conn.execute(RUNS_TABLE)
        conn.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS submitted_by TEXT")  # phase 6
        # Phase 9: how many trail entries of this run are already in the audit log.
        conn.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS audited INT NOT NULL DEFAULT 0")
        # Phase 1 recorded provider errors as 'blocked'; relabel them so they can be retried.
        conn.execute("UPDATE runs SET status = 'failed' WHERE status = 'blocked' AND status_reason LIKE 'error:%%'")
    rt.workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="run")
    rt.deps = Deps(extraction_prompt=load_extraction_prompt())
    rt.graph = build_graph(rt.deps, checkpointer)
    yield
    rt.workers.shutdown(wait=False)
    rt.pool.close()


app = FastAPI(title="warden-backend", lifespan=lifespan)


@app.middleware("http")
async def front_door(request: Request, call_next):
    """Phase 11b-2: on Azure, only requests through API Management or the console get in."""
    request.state.entry = None
    if entry.KEYS and request.url.path not in entry.OPEN_PATHS:
        request.state.entry = entry.which(request.headers.get(entry.HEADER, ""))
        if request.state.entry is None:
            return JSONResponse(status_code=403, content={
                "detail": "come in through the console or the API gateway"})
    return await call_next(request)
if telemetry.configure():   # phase 9b: only when OTEL_EXPORTER_OTLP_ENDPOINT is set
    telemetry.instrument(app)


class SubmitRequest(BaseModel):
    invoice_text: str = Field(min_length=1, max_length=50_000)
    source: Literal["paste", "ocr", "dataset"] = "paste"


class ImageSubmitRequest(BaseModel):
    image_base64: str = Field(min_length=1, max_length=8 * 2**20)
    filename: str | None = Field(default=None, max_length=200)


class ResumeRequest(BaseModel):
    decision: Literal["approve", "reject"]
    note: str = Field(default="", max_length=2000)
    same_vendor_id: int | None = Field(default=None, description=(
        "Only for a first-seen vendor, only with approve: the person confirms this invoice's vendor "
        "is this existing ledger vendor, recorded as an alias so both names share one history."))


def _config(run_id: str) -> dict:
    return {"configurable": {"thread_id": run_id}}


def _sync(run_id: str, values: dict) -> None:
    ext = values.get("extraction") or {}
    with rt.pool.connection() as conn:
        conn.execute(
            # A resumed run first re-emits its paused state; writing that back would reopen
            # the approval for a second click, so 'resuming' only ever moves forward.
            """UPDATE runs SET status = %s, status_reason = %s, vendor_name = %s, total_amount = %s,
                              updated_at = now()
               WHERE run_id = %s AND NOT (status = 'resuming' AND %s = 'pending_human')""",
            (values.get("status", "submitted"), values.get("status_reason"), ext.get("vendor_name"),
             ext.get("total_amount"), run_id, values.get("status")),
        )


# Who acted at each step, for the audit log's "who" column.
STEP_ACTOR = {"ocr": "ocr-sandbox", "privacy": "pii-masking", "guardrail": "guardrail", "extraction": "extraction", "risk_scoring": "risk-scoring",
              "approval": "approval", "critic": "critic", "dry_run": "payment-execution",
              "settlement_hold": "payment-execution", "commit": "payment-execution"}


def _record_steps(run_id: str, values: dict) -> None:
    """Copies new trail entries into the hash-chained audit log, in order, each once.
    If the control plane is down the entries wait and go with the next update."""
    trail = values.get("audit_trail") or []
    with rt.pool.connection() as conn:
        row = conn.execute("SELECT audited FROM runs WHERE run_id = %s", (run_id,)).fetchone()
    done = row["audited"] if row else len(trail)
    ext = values.get("extraction") or {}
    for i in range(done, len(trail)):
        entry = trail[i]
        actor = entry.get("approver") or entry.get("principal") or STEP_ACTOR.get(entry["step"], entry["step"])
        try:
            rt.deps.control.record("run_step", entry | {"vendor": ext.get("vendor_name"),
                                                        "amount": ext.get("total_amount")},
                                   run_id=run_id, actor=str(actor))
        except Exception:
            log.warning("audit log unreachable; %d step(s) of run %s will be sent later", len(trail) - i, run_id)
            return
        with rt.pool.connection() as conn:
            conn.execute("UPDATE runs SET audited = %s WHERE run_id = %s", (i + 1, run_id))


def _drive(run_id: str, graph_input, links=None) -> None:
    kind = "resume" if isinstance(graph_input, Command) else "retry" if graph_input is None else "submit"
    with telemetry.run_span(run_id, kind, links) as span:
        _drive_traced(run_id, graph_input, span)


def _drive_traced(run_id: str, graph_input, span) -> None:
    values: dict = {}
    wake.wake()   # phase 11: on Azure, start any sleeping service before the first step needs it
    try:
        for values in rt.graph.stream(graph_input, _config(run_id), stream_mode="values"):
            _sync(run_id, values)
            _record_steps(run_id, values)
        telemetry.run_ended(span, values)
    except KillSwitchOn as exc:
        telemetry.run_failed(span, "stopped_by_kill_switch", exc)
        # Not an error in the run: someone (or the watchdog) stopped payments. Retry after the reset.
        with rt.pool.connection() as conn:
            conn.execute("UPDATE runs SET status = 'failed', status_reason = %s, updated_at = now() "
                         "WHERE run_id = %s", (f"kill switch: {exc}"[:1000], run_id))
        try:
            rt.deps.control.record("run_stopped", {"reason": str(exc)}, run_id=run_id, actor="kill-switch")
        except Exception:
            log.warning("could not record the stop of run %s", run_id)
    except Exception as exc:
        # A crashed run must not look like it is still working. 'failed' (not 'blocked'):
        # nothing decided against this invoice; a provider or infrastructure error stopped it,
        # and the checkpoint lets POST /runs/{id}/retry continue from the step that failed.
        log.exception("run %s failed", run_id)
        telemetry.run_failed(span, "failed", exc)
        with rt.pool.connection() as conn:
            conn.execute("UPDATE runs SET status = 'failed', status_reason = %s, updated_at = now() "
                         "WHERE run_id = %s", (f"error: {type(exc).__name__}: {exc}"[:1000], run_id))


@app.get("/health")
def health():
    with rt.pool.connection() as conn:
        conn.execute("SELECT 1")
    p = rt.deps.extraction_prompt
    return {
        "status": "ok",
        "extraction_prompt": {"source": p.source, "version": p.version},
        "rules": {
            "routine_amount_threshold": config.ROUTINE_AMOUNT_THRESHOLD,
            "confidence_threshold": config.CONFIDENCE_THRESHOLD,
            "min_prior_payments": config.MIN_PRIOR_PAYMENTS,
            "ledger_currency": config.LEDGER_CURRENCY,
            "settlement_hold_seconds": config.SETTLEMENT_HOLD_SECONDS,
        },
    }


ANYONE = require("submitter", "approver", "controller")
SUBMITTER = require("submitter")
DECIDER = require("approver", "controller")


@app.get("/me")
def me(user: User = Depends(ANYONE)):
    return {"username": user.username, "name": user.name, "roles": sorted(user.roles)}


@app.get("/policies", dependencies=[Depends(ANYONE)])
def policies():
    """The Cedar rules in force, relayed from the policy service, with each role's
    capability manifest: both locks, for the console's governance page."""
    from .capability import MANIFESTS
    manifests = [json.loads(p.read_text()) for p in sorted(MANIFESTS.glob("*.json"))]
    try:
        cedar = rt.deps.policy.policies()
    except Exception as exc:  # the page should say so, not fail
        cedar = {"error": f"policy service unavailable: {exc}"}
    return {"cedar": cedar, "capabilities": manifests}


def _start(initial: dict, user: User, via: str | None = None) -> dict:
    run_id = str(uuid.uuid4())
    wake.wake()   # phase 11: so the submission below reaches the audit log even after a quiet spell
    with rt.pool.connection() as conn:
        conn.execute("INSERT INTO runs (run_id, status, source, submitted_by) VALUES (%s, 'submitted', %s, %s)",
                     (run_id, initial["source"], user.username))
    try:
        rt.deps.control.record("run_submitted", {"source": initial["source"]} | ({"via": via} if via else {}),
                               run_id=run_id, actor=user.username)
    except Exception:
        log.warning("could not record the submission of run %s", run_id)
    rt.workers.submit(_drive, run_id, initial | {"run_id": run_id, "submitted_by": user.username,
                                                 "status": "submitted", "audit_trail": []},
                      telemetry.current_link())
    return {"run_id": run_id, "status": "submitted"}


@app.post("/runs", status_code=202)
def submit(req: SubmitRequest, request: Request, user: User = Depends(SUBMITTER)):
    return _start({"invoice_text": req.invoice_text, "source": req.source}, user, via=request.state.entry)


@app.post("/runs/image", status_code=202)
def submit_image(req: ImageSubmitRequest, request: Request, user: User = Depends(SUBMITTER)):
    """A scanned invoice. The backend never opens the image: the first step sends it to
    the sandboxed OCR service, and the text then goes through the guardrail like pasted text."""
    if len(req.image_base64) * 3 // 4 > config.MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail=f"image larger than {config.MAX_IMAGE_BYTES // 2**20} MB")
    return _start({"invoice_text": "", "image_base64": req.image_base64, "source": "ocr"}, user,
                  via=request.state.entry)


@app.get("/runs", dependencies=[Depends(ANYONE)])
def list_runs(status: str | None = None, limit: int = 50):
    with rt.pool.connection() as conn:
        return conn.execute(
            "SELECT * FROM runs WHERE (%s::text IS NULL OR status = %s) ORDER BY created_at DESC LIMIT %s",
            (status, status, min(limit, 500)),
        ).fetchall()


@app.get("/runs/{run_id}", dependencies=[Depends(ANYONE)])
def get_run(run_id: str):
    snapshot = rt.graph.get_state(_config(run_id))
    if not snapshot.values:
        raise HTTPException(status_code=404, detail="unknown run")
    with rt.pool.connection() as conn:
        row = conn.execute("SELECT * FROM runs WHERE run_id = %s", (run_id,)).fetchone()
    return {
        "run": row,  # service status (failed, resuming, retrying) lives here, not in the graph state
        # The image and the placeholder mapping stay in the backend; masked_text is "what the models saw".
        "state": {k: v for k, v in snapshot.values.items() if k not in ("image_base64", "pii_mapping")},
        "next": list(snapshot.next),
        "awaiting_human": [i.value for i in snapshot.interrupts],
    }


@app.post("/runs/{run_id}/resume", status_code=202)
def resume(run_id: str, req: ResumeRequest, user: User = Depends(DECIDER)):
    snapshot = rt.graph.get_state(_config(run_id))
    if not snapshot.interrupts:
        raise HTTPException(status_code=409, detail="run is not waiting for a human")
    wake.wake()   # phase 11: the services may have gone to sleep while the run waited for a person
    if req.decision == "approve":
        # Ask Cedar before resuming, so a person who may not approve this one (amount over their
        # tier, or their own invoice) is told now and the run stays waiting for someone who may.
        grant = rt.deps.policy.authorize(("User", user.username), "ApprovePayment",
                                         payment=payment_facts(snapshot.values), roles=sorted(user.roles))
        if grant["decision"] != "Allow":
            try:  # refusals go on the record too
                rt.deps.control.record("approval_refused", {"reasons": grant["reasons"], "rules": grant["policies"]},
                                       run_id=run_id, actor=user.username)
            except Exception:
                log.warning("could not record a refused approval on run %s", run_id)
            raise HTTPException(status_code=403, detail="you can't approve this one: " + "; ".join(grant["reasons"]))
    if req.same_vendor_id is not None:
        _confirm_alias(run_id, snapshot.values, req, user)
    with rt.pool.connection() as conn:  # atomic claim, so two clicks can't resume the same run twice
        claimed = conn.execute("UPDATE runs SET status = 'resuming', updated_at = now() "
                               "WHERE run_id = %s AND status = 'pending_human' RETURNING run_id", (run_id,)).fetchone()
    if not claimed:
        raise HTTPException(status_code=409, detail="run is already being resumed")
    answer = req.model_dump() | {"approver": user.username, "approver_name": user.name, "roles": sorted(user.roles)}
    rt.workers.submit(_drive, run_id, Command(resume=answer), telemetry.current_link())
    return {"run_id": run_id, "status": "resuming"}


def _confirm_alias(run_id: str, values: dict, req: ResumeRequest, user: User) -> None:
    if req.decision != "approve":
        raise HTTPException(status_code=422, detail="a vendor can only be linked when approving")
    if (values.get("vendor") or {}).get("known", True):
        raise HTTPException(status_code=409, detail="only a first-seen vendor can be linked to an existing one")
    name = values["extraction"]["vendor_name"]
    grant = rt.deps.policy.authorize(("User", user.username), "ConfirmVendorAlias", vendor=name,
                                     roles=sorted(user.roles))
    if grant["decision"] != "Allow":
        raise HTTPException(status_code=403, detail="; ".join(grant["reasons"]))
    ok, result = rt.deps.ledger_for("human-review").confirm_alias(name, req.same_vendor_id, user.username, run_id)
    if not ok:  # the run stays pending_human, so the person can choose again
        raise HTTPException(status_code=409, detail=f"could not link the vendor: {result}")


@app.post("/runs/{run_id}/retry", status_code=202, dependencies=[Depends(ANYONE)])
def retry(run_id: str):
    """Continues a failed run from the step that raised. Steps that already
    finished are not repeated: their results are in the checkpoint."""
    with rt.pool.connection() as conn:
        claimed = conn.execute("UPDATE runs SET status = 'retrying', status_reason = NULL, updated_at = now() "
                               "WHERE run_id = %s AND status = 'failed' RETURNING run_id", (run_id,)).fetchone()
    if not claimed:
        raise HTTPException(status_code=409, detail="only a failed run can be retried")
    rt.workers.submit(_drive, run_id, None, telemetry.current_link())
    return {"run_id": run_id, "status": "retrying"}


@app.get("/inbox", dependencies=[Depends(ANYONE)])
def inbox(source: Literal["sroie", "cord"] = "sroie", split: str = "test", limit: int = 20):
    """Real dataset receipts loaded by the data loader, for the console's submit page."""
    with rt.pool.connection() as conn:
        exists = conn.execute("SELECT to_regclass('invoice_inbox') AS t").fetchone()["t"]
        if not exists:
            return []
        return conn.execute(
            """SELECT external_key, source, split, left(text, 160) AS preview,
                      ground_truth->'entities'->>'company' AS labelled_vendor,
                      ground_truth->'entities'->>'total' AS labelled_total
               FROM invoice_inbox WHERE source = %s AND split = %s ORDER BY id LIMIT %s""",
            (source, split, min(limit, 200)),
        ).fetchall()


@app.post("/inbox/{external_key}/submit", status_code=202)
def submit_from_inbox(external_key: str, user: User = Depends(SUBMITTER)):
    with rt.pool.connection() as conn:
        row = conn.execute("SELECT text FROM invoice_inbox WHERE external_key = %s", (external_key,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="no such document in the inbox")
    return _start({"invoice_text": row["text"], "source": "dataset"}, user)
