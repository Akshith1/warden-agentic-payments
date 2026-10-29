"""Every agent calls a gateway route name, never a provider model ID, and
gets back a validated Pydantic object. Instructor re-asks the model when the
output fails the schema."""
from functools import cache
from typing import TypeVar

import instructor
from openai import OpenAI
from pydantic import BaseModel

from . import config

T = TypeVar("T", bound=BaseModel)


@cache
def _client() -> instructor.Instructor:
    # JSON mode, not tool calling: Groq validates tool arguments server-side and answers a
    # mismatch with a hard 400, which bypasses Instructor's re-ask. In JSON mode Pydantic
    # validates here, so a schema miss gets retried with the error fed back to the model.
    return instructor.from_openai(OpenAI(base_url=config.LITELLM_URL, api_key=config.LITELLM_API_KEY),
                                  mode=instructor.Mode.JSON)


def structured(agent: str, route: str, system: str, user: str, response_model: type[T]) -> T:
    return _client().chat.completions.create(
        model=route,
        response_model=response_model,
        max_retries=config.LLM_MAX_RETRIES,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        user=agent,  # LiteLLM attributes spend to this end-user tag
    )
