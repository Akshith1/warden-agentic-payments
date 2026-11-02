"""Client for warden-policy. Fails closed: if the policy service can't answer,
the call raises and the run stops as 'failed', to be retried once it is back."""
import httpx

from . import config


class PolicyClient:
    def __init__(self, base_url: str | None = None, timeout: float = 10.0):
        self._http = httpx.Client(base_url=base_url or config.POLICY_URL, timeout=timeout)

    def authorize(self, principal: tuple[str, str], action: str, *, payment: dict | None = None,
                  vendor: str | None = None) -> dict:
        body = {"principal": {"type": principal[0], "id": principal[1]}, "action": action}
        if payment is not None:
            body["payment"] = payment
        if vendor is not None:
            body["vendor"] = vendor
        r = self._http.post("/authorize", json=body)
        r.raise_for_status()
        return r.json()

    def policies(self) -> dict:
        r = self._http.get("/policies")
        r.raise_for_status()
        return r.json()
