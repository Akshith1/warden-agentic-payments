"""Client for warden-pii. Fails closed: if personal data can't be masked, no
model sees the document; the run fails and can be retried."""
import httpx

from . import config


class PiiClient:
    def __init__(self, base_url: str | None = None, timeout: float = 30.0):
        self._http = httpx.Client(base_url=base_url or config.PII_URL, timeout=timeout)

    def mask(self, text: str) -> dict:
        r = self._http.post("/mask", json={"text": text})
        r.raise_for_status()
        return r.json()
