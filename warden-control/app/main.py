"""warden-control: the kill switch and the audit log (phase 9).

The kill switch stops payments at three points: the policy service refuses
everything while it is on, the API refuses a person's approval, and the
payment step checks it once more at the end of the settlement hold, right
before money moves. A stopped run fails and can be retried after the reset.

People trip and reset it from the console with their Keycloak sign-in:
approvers and controllers may stop, only controllers may resume. It also
trips by itself (see `watch`)."""
import json
import os
import re
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import config, worm
from .auth import User, require
from .store import Store


class State:
    store: Store
    copier: "worm.Copier | None" = None


state = State()


@asynccontextmanager
async def lifespan(_app):
    state.store = Store(config.CONTROL_DB)
    # Phase 11b: on Azure, every entry is also copied to write-once Blob storage.
    state.copier, stop = worm.start(state.store)
    if state.copier:
        state.store.on_append.append(state.copier.wake.set)
    yield
    stop.set()


app = FastAPI(title="warden-control", lifespan=lifespan)
ANYONE = require("submitter", "approver", "controller")


class Event(BaseModel):
    type: str = Field(min_length=1, max_length=64)
    run_id: str | None = None
    actor: str | None = Field(default=None, max_length=200)
    payload: dict = Field(default_factory=dict)


class Spend(BaseModel):
    agent: str = Field(min_length=1, max_length=64)
    route: str | None = None
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    at: str


class Trip(BaseModel):
    scope: Literal["global", "agent"] = "global"
    target: str = Field(default="*", min_length=1, max_length=100)
    reason: str = Field(min_length=3, max_length=500)


class Reset(BaseModel):
    scope: Literal["global", "agent"] = "global"
    target: str = Field(default="*", min_length=1, max_length=100)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/status")
def status():
    """Read by the policy service and the backend before every payment decision."""
    return state.store.status()


@app.post("/kill-switch/trip")
def trip(req: Trip, user: User = Depends(require("approver", "controller"))):
    target = "*" if req.scope == "global" else req.target
    if state.store.trip(req.scope, target, req.reason, user.username):
        state.store.append("kill_switch_tripped", {"scope": req.scope, "target": target, "reason": req.reason},
                           actor=user.username)
    return state.store.status()


@app.post("/kill-switch/reset")
def reset(req: Reset, user: User = Depends(require("controller"))):
    target = "*" if req.scope == "global" else req.target
    if not state.store.reset(req.scope, target):
        raise HTTPException(status_code=409, detail="that switch isn't on")
    state.store.append("kill_switch_reset", {"scope": req.scope, "target": target}, actor=user.username)
    return state.store.status()


@app.post("/events", status_code=201)
def record(event: Event):
    """Appends one event to the audit log, then checks the automatic triggers.
    Called by the backend for every step of every run."""
    written = state.store.append(event.type, event.payload, run_id=event.run_id, actor=event.actor)
    watch(event)
    return written


def watch(event: Event) -> None:
    store = state.store
    text = json.dumps(event.payload).upper()
    hit = next((c for c in config.CANARY_VENDORS if re.search(rf"\b{re.escape(c)}\b", text)), None)
    if hit and store.trip("global", "*", f"canary vendor {hit} seen in run {event.run_id}", "watchdog"):
        store.append("kill_switch_tripped", {"scope": "global", "target": "*", "automatic": True,
                                             "reason": f"canary vendor {hit} seen"}, run_id=event.run_id,
                     actor="watchdog")

    p = event.payload
    # Phase 7b: the data-quality service found the ledger or vendor history wrong. Paying on
    # numbers that don't add up is worse than not paying, so everything stops until a
    # controller has looked. Sent again on every run while it stays wrong, so a reset made
    # before the data is fixed trips again at the next check.
    if event.type == "data_quality" and p.get("critical_failed"):
        titles = [c["title"] for c in p.get("failed_checks", []) if c.get("id") in p["critical_failed"]]
        first = titles[0] if titles else p["critical_failed"][0]
        more = f" (and {len(p['critical_failed']) - 1} more)" if len(p["critical_failed"]) > 1 else ""
        reason = f"data-quality check failed: {first}{more}"
        if store.trip("global", "*", reason, "watchdog"):
            store.append("kill_switch_tripped", {"scope": "global", "target": "*", "automatic": True,
                                                 "reason": reason}, actor="watchdog")

    if event.type == "run_step" and p.get("step") == "policy" and p.get("decision") == "Deny" \
            and str(p.get("principal", "")).startswith("Agent:"):
        agent = p["principal"].split(":", 1)[1]
        refusals = [e for e in store.recent("run_step", config.DENIAL_WINDOW_MINUTES)
                    if e["payload"].get("step") == "policy" and e["payload"].get("decision") == "Deny"
                    and e["payload"].get("principal") == f"Agent:{agent}"]
        if len(refusals) >= config.DENIAL_LIMIT:
            reason = f"{len(refusals)} policy refusals in {config.DENIAL_WINDOW_MINUTES} minutes"
            if store.trip("agent", agent, reason, "watchdog"):
                store.append("kill_switch_tripped", {"scope": "agent", "target": agent, "automatic": True,
                                                     "reason": reason}, actor="watchdog")


@app.get("/audit", dependencies=[Depends(ANYONE)])
def audit(limit: int = 100, run_id: str | None = None):
    return state.store.entries(min(limit, 1000), run_id)


@app.get("/audit/worm", dependencies=[Depends(ANYONE)])
def audit_worm():
    """Phase 11b: the database checked against the write-once copy in Blob storage."""
    if state.copier is None:
        return {"enabled": False}
    try:
        return {"enabled": True, "last_copy_error": state.copier.last_error} | state.copier.compare()
    except Exception as exc:  # noqa: BLE001 - reported to the person asking
        raise HTTPException(status_code=502, detail=f"can't read the write-once copy: {type(exc).__name__}") from exc


@app.get("/audit/verify", dependencies=[Depends(ANYONE)])
def verify():
    return state.store.verify()


# ---- spend per agent (phase 8): written by the LLM gateway after every call ----

@app.post("/spend", status_code=201)
def add_spend(s: Spend):
    state.store.add_spend(s.agent, s.route, s.prompt_tokens, s.completion_tokens, s.cost_usd, s.at)
    return {"ok": True}


@app.get("/spend/today")
def spend_today(agent: str):
    """Read by the gateway before every call, to enforce the agent's daily budget."""
    return state.store.spend_today(agent)


@app.get("/spend", dependencies=[Depends(ANYONE)])
def spend(days: int = 7):
    """Per agent: today's use against its budget, and the last `days` days by route."""
    import yaml
    limits = {}
    if os.path.exists(config.AGENTS_FILE):
        with open(config.AGENTS_FILE) as f:
            limits = yaml.safe_load(f)["agents"]
    today = [state.store.spend_today(a) | {"daily_tokens": rules["daily_tokens"], "routes": rules["routes"]}
             for a, rules in limits.items()]
    return {"today": today, "by_day": state.store.spend_by_day(min(max(days, 1), 90))}
