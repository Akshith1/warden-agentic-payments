import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from . import config
from .agents import Deps
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
        # Phase 1 recorded provider errors as 'blocked'; relabel them so they can be retried.
        conn.execute("UPDATE runs SET status = 'failed' WHERE status = 'blocked' AND status_reason LIKE 'error:%%'")
    rt.workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="run")
    rt.deps = Deps(extraction_prompt=load_extraction_prompt())
    rt.graph = build_graph(rt.deps, checkpointer)
    yield
    rt.workers.shutdown(wait=False)
    rt.pool.close()


app = FastAPI(title="warden-backend", lifespan=lifespan)


class SubmitRequest(BaseModel):
    invoice_text: str = Field(min_length=1, max_length=50_000)
    source: Literal["paste", "ocr", "dataset"] = "paste"


class ResumeRequest(BaseModel):
    decision: Literal["approve", "reject"]
    approver: str = Field(min_length=1, description="Phase 6 takes this from the Keycloak token instead")
    note: str = ""


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


def _drive(run_id: str, graph_input) -> None:
    try:
        for values in rt.graph.stream(graph_input, _config(run_id), stream_mode="values"):
            _sync(run_id, values)
    except Exception as exc:
        # A crashed run must not look like it is still working. 'failed' (not 'blocked'):
        # nothing decided against this invoice; a provider or infrastructure error stopped it,
        # and the checkpoint lets POST /runs/{id}/retry continue from the step that failed.
        log.exception("run %s failed", run_id)
        with rt.pool.connection() as conn:
            conn.execute("UPDATE runs SET status = 'failed', status_reason = %s, updated_at = now() "
                         "WHERE run_id = %s", (f"error: {type(exc).__name__}: {exc}"[:1000], run_id))


@app.get("/health")
def health():
    with rt.pool.connection() as conn:
        conn.execute("SELECT 1")
    p = rt.deps.extraction_prompt
    return {"status": "ok", "extraction_prompt": {"source": p.source, "version": p.version}}


@app.post("/runs", status_code=202)
def submit(req: SubmitRequest):
    run_id = str(uuid.uuid4())
    with rt.pool.connection() as conn:
        conn.execute("INSERT INTO runs (run_id, status, source) VALUES (%s, 'submitted', %s)", (run_id, req.source))
    initial = {"run_id": run_id, "invoice_text": req.invoice_text, "source": req.source,
               "status": "submitted", "audit_trail": []}
    rt.workers.submit(_drive, run_id, initial)
    return {"run_id": run_id, "status": "submitted"}


@app.get("/runs")
def list_runs(status: str | None = None, limit: int = 50):
    with rt.pool.connection() as conn:
        return conn.execute(
            "SELECT * FROM runs WHERE (%s::text IS NULL OR status = %s) ORDER BY created_at DESC LIMIT %s",
            (status, status, min(limit, 500)),
        ).fetchall()


@app.get("/runs/{run_id}")
def get_run(run_id: str):
    snapshot = rt.graph.get_state(_config(run_id))
    if not snapshot.values:
        raise HTTPException(status_code=404, detail="unknown run")
    return {
        "state": snapshot.values,
        "next": list(snapshot.next),
        "awaiting_human": [i.value for i in snapshot.interrupts],
    }


@app.post("/runs/{run_id}/resume", status_code=202)
def resume(run_id: str, req: ResumeRequest):
    snapshot = rt.graph.get_state(_config(run_id))
    if not snapshot.interrupts:
        raise HTTPException(status_code=409, detail="run is not waiting for a human")
    with rt.pool.connection() as conn:  # atomic claim, so two clicks can't resume the same run twice
        claimed = conn.execute("UPDATE runs SET status = 'resuming', updated_at = now() "
                               "WHERE run_id = %s AND status = 'pending_human' RETURNING run_id", (run_id,)).fetchone()
    if not claimed:
        raise HTTPException(status_code=409, detail="run is already being resumed")
    rt.workers.submit(_drive, run_id, Command(resume=req.model_dump()))
    return {"run_id": run_id, "status": "resuming"}


@app.post("/runs/{run_id}/retry", status_code=202)
def retry(run_id: str):
    """Continues a failed run from the step that raised. Steps that already
    finished are not repeated: their results are in the checkpoint."""
    with rt.pool.connection() as conn:
        claimed = conn.execute("UPDATE runs SET status = 'retrying', status_reason = NULL, updated_at = now() "
                               "WHERE run_id = %s AND status = 'failed' RETURNING run_id", (run_id,)).fetchone()
    if not claimed:
        raise HTTPException(status_code=409, detail="only a failed run can be retried")
    rt.workers.submit(_drive, run_id, None)
    return {"run_id": run_id, "status": "retrying"}
