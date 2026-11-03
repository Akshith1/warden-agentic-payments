"""How this service proves who it is to the ledger (phase 6b).

With LEDGER_AUTH=spiffe it asks its SPIRE agent (SPIFFE_ENDPOINT_SOCKET) for a
JWT-SVID with the ledger as audience, keeps it until a minute before it
expires, and sends it as a bearer token. SPIRE issued it only after checking
which container asked, so nothing here is a secret that could be copied out
of a file. With LEDGER_AUTH=keys (the default) it sends the shared API key."""
import os
import threading
import time

MODE = os.environ.get("LEDGER_AUTH", "keys")
AUDIENCE = os.environ.get("LEDGER_AUDIENCE", "warden-ledger")


class Identity:
    def __init__(self, audience: str = AUDIENCE, client=None):
        self.audience, self._client = audience, client
        self._token, self._expiry = None, 0.0
        self._lock = threading.Lock()

    def _workload_api(self):
        if self._client is None:
            from spiffe import WorkloadApiClient
            self._client = WorkloadApiClient(default_timeout=5)
        return self._client

    def token(self) -> str:
        with self._lock:
            if self._token is None or time.time() > self._expiry - 60:
                svid = self._workload_api().fetch_jwt_svid(audience={self.audience})
                self._token, self._expiry = svid.token, float(svid.expiry)
            return self._token

    def headers(self, key: str) -> dict:
        """The credential to send: this workload's identity, or the shared key in keys mode."""
        return {"Authorization": f"Bearer {self.token()}"} if MODE == "spiffe" else {"X-API-Key": key}
