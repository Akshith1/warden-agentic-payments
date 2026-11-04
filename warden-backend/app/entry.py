"""Phase 11b-2: which front door a request came through.

On Azure the backend has to be reachable from the internet, because API
Management's Consumption tier can't join a private network. So that nobody
goes round API Management (its token check, subscription keys and rate
limits), every request must carry X-Warden-Entry with a key naming its front
door: API Management adds one, the console's server adds another. A request
with neither is refused before anything else, whatever token it carries.

ENTRY_KEYS lists them as name:key pairs ("console:...,apim:..."). Unset (the
local stack, where nothing outside the Docker network can reach the backend),
there is no check."""
import os
import secrets

HEADER = "x-warden-entry"


def _parse(raw: str) -> dict[str, str]:
    pairs = (item.split(":", 1) for item in raw.split(",") if ":" in item)
    return {name.strip(): key.strip() for name, key in pairs if name.strip() and key.strip()}


KEYS = _parse(os.environ.get("ENTRY_KEYS", ""))
OPEN_PATHS = {"/health"}


def which(presented: str, keys: dict[str, str] | None = None) -> str | None:
    """The name of the front door whose key was presented, or None."""
    for name, key in (KEYS if keys is None else keys).items():
        if presented and secrets.compare_digest(presented.encode(), key.encode()):
            return name
    return None
