"""How this service proves who it is to the ledger (phase 6b).

With LEDGER_AUTH=spiffe it asks its SPIRE agent (SPIFFE_ENDPOINT_SOCKET) for a
JWT-SVID with the ledger as audience, keeps it until a minute before it
expires, and sends it as a bearer token. SPIRE issued it only after checking
which container asked, so nothing here is a secret that could be copied out
of a file. With LEDGER_AUTH=entra (phase 11, Azure Container Apps) it asks the
platform's managed identity endpoint for an Entra ID token instead, for the
ledger's app registration; AZURE_CLIENT_ID picks which of the app's identities.
With LEDGER_AUTH=keys (the default) it sends the shared API key."""
import os
import threading
import time

MODE = os.environ.get("LEDGER_AUTH", "keys")
AUDIENCE = os.environ.get("LEDGER_AUDIENCE", "warden-ledger")


class ManagedIdentity:
    """Container Apps' managed identity endpoint (IDENTITY_ENDPOINT and IDENTITY_HEADER are
    set by the platform). Returns (token, expiry) like a JWT-SVID, so Identity caches it the same way."""

    def __init__(self, client_id: str | None = None, http=None):
        self.client_id = client_id if client_id is not None else os.environ.get("AZURE_CLIENT_ID", "")
        self._http = http

    def fetch(self, resource: str) -> tuple[str, float]:
        import httpx
        http = self._http or httpx
        params = {"resource": resource, "api-version": "2019-08-01"}
        if self.client_id:
            params["client_id"] = self.client_id
        r = http.get(os.environ["IDENTITY_ENDPOINT"], params=params,
                     headers={"X-IDENTITY-HEADER": os.environ["IDENTITY_HEADER"]}, timeout=10)
        r.raise_for_status()
        body = r.json()
        return body["access_token"], float(body["expires_on"])


class Identity:
    def __init__(self, audience: str = AUDIENCE, client=None, managed_identity: ManagedIdentity | None = None):
        self.audience, self._client, self._mi = audience, client, managed_identity
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
                if MODE == "entra":
                    self._mi = self._mi or ManagedIdentity()
                    self._token, self._expiry = self._mi.fetch(self.audience)
                else:
                    svid = self._workload_api().fetch_jwt_svid(audience={self.audience})
                    self._token, self._expiry = svid.token, float(svid.expiry)
            return self._token

    def headers(self, key: str) -> dict:
        """The credential to send: this workload's identity, or the shared key in keys mode."""
        return {"Authorization": f"Bearer {self.token()}"} if MODE in ("spiffe", "entra") else {"X-API-Key": key}
