"""Warden's rules at the LLM gateway (phase 8), as LiteLLM extension points.

`user_api_key_auth` runs on every request. It maps the key to an agent and
refuses the call unless the route is one the agent may use, the agent isn't
paused by the kill switch, and the agent is under today's token budget.
`spend_logger` runs after every successful call and records the agent's
tokens and cost in warden-control, which is where the budget is read from.

The agents can't get round this: they hold only their own key, and the
backend no longer has the gateway's master key."""
import hmac
import json
import os
from datetime import datetime, timezone

import httpx
import yaml
from fastapi import Request
from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy._types import LitellmUserRoles, ProxyErrorTypes, ProxyException, UserAPIKeyAuth

AGENTS_FILE = os.environ.get("WARDEN_AGENTS_FILE", "/etc/litellm/agents.yaml")
KEYS_FILE = os.environ.get("WARDEN_AGENT_KEYS_FILE", "/run/agent-keys/agent-keys.json")
CONTROL_URL = os.environ.get("CONTROL_URL", "http://warden-control:8000")
MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "")

_agents: dict | None = None
_keys: dict | None = None
_http = httpx.AsyncClient(timeout=5)


def agents() -> dict:
    global _agents
    if _agents is None:
        with open(AGENTS_FILE) as f:
            _agents = yaml.safe_load(f)["agents"]
    return _agents


def keys() -> dict:
    global _keys
    if _keys is None:
        with open(KEYS_FILE) as f:
            _keys = json.load(f)  # {agent: key}
    return _keys


def refuse(message: str, code: int) -> ProxyException:
    return ProxyException(message=message, type=ProxyErrorTypes.auth_error, param="api_key", code=code)


async def _body(request: Request) -> dict:
    from litellm.proxy.common_utils.http_parsing_utils import _read_request_body
    return await _read_request_body(request) or {}


async def user_api_key_auth(request: Request, api_key: str) -> UserAPIKeyAuth:
    if MASTER_KEY and hmac.compare_digest(api_key, MASTER_KEY):
        return UserAPIKeyAuth(api_key=api_key, user_role=LitellmUserRoles.PROXY_ADMIN, key_alias="admin")
    agent = next((a for a, k in keys().items() if hmac.compare_digest(api_key, k)), None)
    if agent is None or agent not in agents():
        raise refuse("unknown key", 401)
    rules = agents()[agent]

    route = (await _body(request)).get("model")
    if route is not None and route not in rules["routes"]:
        await _record_refusal(agent, route, "route not allowed")
        raise refuse(f"the {agent} agent may not use the {route} route (allowed: {', '.join(rules['routes'])})", 403)

    try:  # fail closed: no answer from the control plane, no model call
        status = (await _http.get(f"{CONTROL_URL}/status")).json()
        spent = (await _http.get(f"{CONTROL_URL}/spend/today", params={"agent": agent})).json()
    except (httpx.HTTPError, ValueError) as e:
        raise refuse(f"control plane unreachable, so no model calls: {e}", 503) from e
    if agent in status.get("paused_agents", []):
        raise refuse(f"the {agent} agent is paused by the kill switch", 403)
    if spent["tokens"] >= rules["daily_tokens"]:
        await _record_refusal(agent, route, "daily token budget used up")
        raise refuse(f"the {agent} agent has used its daily budget ({spent['tokens']:,} of "
                     f"{rules['daily_tokens']:,} tokens); it resets at midnight UTC", 429)
    return UserAPIKeyAuth(api_key=api_key, key_alias=agent, metadata={"warden_agent": agent})


async def _record_refusal(agent: str, route: str | None, reason: str) -> None:
    try:
        await _http.post(f"{CONTROL_URL}/events", json={"type": "gateway_refused", "actor": agent,
                                                         "payload": {"agent": agent, "route": route,
                                                                     "reason": reason}})
    except httpx.HTTPError:
        pass  # the refusal itself still stands


class SpendLogger(CustomLogger):
    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        meta = (kwargs.get("litellm_params") or {}).get("metadata") or {}
        agent = meta.get("user_api_key_alias") or (meta.get("user_api_key_metadata") or {}).get("warden_agent")
        if not agent or agent == "admin":
            return
        usage = getattr(response_obj, "usage", None)
        record = {"agent": agent, "route": kwargs.get("model_group") or meta.get("model_group") or kwargs.get("model"),
                  "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                  "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
                  "cost_usd": kwargs.get("response_cost"),
                  "at": datetime.now(timezone.utc).isoformat()}
        try:
            await _http.post(f"{CONTROL_URL}/spend", json=record)
        except httpx.HTTPError:
            pass  # spend not recorded is logged by LiteLLM; the call already happened


spend_logger = SpendLogger()
