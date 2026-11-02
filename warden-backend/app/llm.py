"""Every agent calls a gateway route name, never a provider model ID, and
gets back a validated Pydantic object. Instructor re-asks the model when the
output fails the schema.

Phase 8: each agent calls with its own gateway key, read from the agent-keys
volume, so the gateway knows which agent is calling and applies that agent's
routes and daily budget. The backend doesn't hold the gateway's master key."""
import json
import os
from functools import cache
from typing import TypeVar

import instructor
from openai import OpenAI
from pydantic import BaseModel

from . import config, telemetry

T = TypeVar("T", bound=BaseModel)


@cache
def _agent_keys() -> dict:
    if os.path.exists(config.AGENT_KEYS_FILE):
        with open(config.AGENT_KEYS_FILE) as f:
            return json.load(f)
    return {}


def key_for(agent: str) -> str:
    keys = _agent_keys()
    if agent in keys:
        return keys[agent]
    if config.LITELLM_API_KEY:  # no keys volume: a local run with a single gateway key
        return config.LITELLM_API_KEY
    raise RuntimeError(f"no gateway key for the {agent} agent in {config.AGENT_KEYS_FILE}")


@cache
def _client(agent: str) -> instructor.Instructor:
    # JSON mode, not tool calling: Groq validates tool arguments server-side and answers a
    # mismatch with a hard 400, which bypasses Instructor's re-ask. In JSON mode Pydantic
    # validates here, so a schema miss gets retried with the error fed back to the model.
    return instructor.from_openai(OpenAI(base_url=config.LITELLM_URL, api_key=key_for(agent)),
                                  mode=instructor.Mode.JSON)


def structured(agent: str, route: str, system: str, user: str, response_model: type[T]) -> T:
    with telemetry.model_call(agent, route) as record:   # phase 9b: a span, latency and tokens
        result, completion = _client(agent).chat.completions.create_with_completion(
            model=route,
            response_model=response_model,
            max_retries=config.LLM_MAX_RETRIES,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            user=agent,  # LiteLLM attributes spend to this end-user tag
        )
        usage = getattr(completion, "usage", None)
        record.update(model=getattr(completion, "model", None),
                      input_tokens=getattr(usage, "prompt_tokens", None),
                      output_tokens=getattr(usage, "completion_tokens", None))
    return result
