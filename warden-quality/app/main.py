"""warden-quality: Great Expectations checks on the ledger, vendor history and
loaded data (phase 7b). Read-only; it can stop payments only by telling the
control plane, which trips the kill switch on a critical failure.

    GET  /health     open
    GET  /checks     what is checked and why (signed in)
    GET  /results    the latest run (signed in)
    POST /run        run now; records who asked when a valid sign-in comes with it.
                     Open inside the Docker network (no published port), so the data
                     loader can run the checks after a load."""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import auth, config, runner
from .checks import CHECKS

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("warden.quality")
ANYONE = auth.require("submitter", "approver", "controller")
_optional = HTTPBearer(auto_error=False)


async def _schedule():
    await asyncio.sleep(config.FIRST_RUN_DELAY_SECONDS)
    while True:
        try:
            result = await asyncio.to_thread(runner.run_checks, "schedule")
            log.info("checks: %s passed, %s failed, %s errors", result["passed"], result["failed"], result["errors"])
        except Exception:  # noqa: BLE001 - keep the schedule alive; the next run may succeed
            log.exception("data-quality run failed")
        await asyncio.sleep(config.INTERVAL_MINUTES * 60)


@asynccontextmanager
async def lifespan(_app):
    task = asyncio.create_task(_schedule()) if config.INTERVAL_MINUTES > 0 else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="warden-quality", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok", "checks": len(CHECKS)}


@app.get("/checks", dependencies=[Depends(ANYONE)])
def checks():
    return [{"id": c.id, "suite": c.suite, "severity": c.severity, "table": c.table, "title": c.title,
             "why": c.why} for c in CHECKS]


@app.get("/results", dependencies=[Depends(ANYONE)])
def results():
    latest = runner.latest()
    if latest is None:
        raise HTTPException(status_code=404, detail="no run yet: the first one starts shortly after startup")
    return latest


@app.post("/run")
async def run(credentials: HTTPAuthorizationCredentials | None = Depends(_optional)):
    who = auth.verify(credentials.credentials).username if credentials else "service"
    return await asyncio.to_thread(runner.run_checks, who)
