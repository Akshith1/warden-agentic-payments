"""Who may read and write the ledger.

Three modes, chosen with LEDGER_AUTH:

* keys (the code's default, kept for local tests): a shared API key per
  scope, sent as X-API-Key. Compose no longer configures a write key, so in
  Compose this mode accepts no writes at all.
* spiffe (phase 6b, Compose's default): callers send a JWT-SVID, a short-lived token SPIRE signs
  for a workload after checking which container is asking. The ledger asks
  its own SPIRE agent to validate it, then allows the scope only to the
  SPIFFE IDs listed for it. Writes take no key at all: a leaked write key
  stops working. The console still reads with the read key, because its Node
  server has no maintained SPIFFE library; that is the one key left.

* entra (phase 11, the Azure profile): Container Apps has no SPIRE agent, so
  each caller sends an Entra ID token its managed identity got for the
  ledger's app registration. The ledger checks the signature against Entra's
  published keys, the tenant, the audience and expiry, and allows the scope
  only to the managed identities (client IDs) listed for it. Again nothing
  that could be copied out of a file.

Validated tokens are remembered until they expire, so the agent is asked once
per token rather than once per request."""
import os
import secrets
import threading
import time
from collections import OrderedDict

from fastapi import Header, HTTPException

MODE = os.environ.get("LEDGER_AUTH", "keys")
WRITE_KEY = os.environ.get("LEDGER_WRITE_KEY", "")
LOOKUP_KEY = os.environ.get("VENDOR_LOOKUP_KEY", "")
AUDIENCE = os.environ.get("LEDGER_AUDIENCE", "warden-ledger")
TRUST_DOMAIN = os.environ.get("SPIFFE_TRUST_DOMAIN", "warden.local")


def _ids(name: str, default: str) -> frozenset[str]:
    return frozenset(i.strip() for i in os.environ.get(name, default).split(",") if i.strip())


TENANT = os.environ.get("ENTRA_TENANT_ID", "")
ENTRA_ISSUERS = frozenset({f"https://login.microsoftonline.com/{TENANT}/v2.0", f"https://sts.windows.net/{TENANT}/"})
ENTRA_JWKS_URL = os.environ.get("ENTRA_JWKS_URL", f"https://login.microsoftonline.com/{TENANT}/discovery/v2.0/keys")

WRITERS = _ids("LEDGER_WRITERS", f"spiffe://{TRUST_DOMAIN}/backend")
READERS = _ids("LEDGER_READERS", f"spiffe://{TRUST_DOMAIN}/backend,spiffe://{TRUST_DOMAIN}/policy") | WRITERS


class _Validated:
    """A small, bounded memory of tokens the agent already accepted: token -> (spiffe id, expiry)."""

    def __init__(self, size: int = 256):
        self._items: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._size, self._lock = size, threading.Lock()

    def get(self, token: str) -> str | None:
        with self._lock:
            hit = self._items.get(token)
            if hit and hit[1] > time.time() + 5:
                return hit[0]
            self._items.pop(token, None)
            return None

    def put(self, token: str, spiffe_id: str, expiry: float) -> None:
        with self._lock:
            self._items[token] = (spiffe_id, expiry)
            while len(self._items) > self._size:
                self._items.popitem(last=False)


_validated = _Validated()
_client = None


def _workload_api():
    global _client
    if _client is None:
        from spiffe import WorkloadApiClient
        _client = WorkloadApiClient(default_timeout=5)   # SPIFFE_ENDPOINT_SOCKET names the agent
    return _client


_entra_keys = None


def _entra_caller(token: str) -> tuple[str, float]:
    """The managed identity (client ID) an Entra token proves. Version 1 and 2 tokens both
    work: they differ in the issuer, the audience's form and the claim naming the caller."""
    import jwt
    global _entra_keys
    if _entra_keys is None:
        _entra_keys = jwt.PyJWKClient(ENTRA_JWKS_URL, cache_keys=True, lifespan=3600, timeout=10)
    key = _entra_keys.get_signing_key_from_jwt(token).key
    claims = jwt.decode(token, key, algorithms=["RS256"], audience=[AUDIENCE, f"api://{AUDIENCE}"],
                        options={"require": ["exp", "iss", "aud"], "verify_iss": False}, leeway=30)
    if not TENANT or claims["iss"] not in ENTRA_ISSUERS or claims.get("tid") != TENANT:
        raise jwt.InvalidIssuerError("token from another tenant")
    who = claims.get("azp") or claims.get("appid")
    if not who:
        raise jwt.MissingRequiredClaimError("azp")
    return who, float(claims["exp"])


def caller(token: str) -> str:
    """Who a token proves: a SPIFFE ID checked by this host's SPIRE agent, or (entra mode)
    a managed identity's client ID checked against Entra ID's keys."""
    known = _validated.get(token)
    if known:
        return known
    if MODE == "entra":
        try:
            who, expiry = _entra_caller(token)
        except Exception as e:  # noqa: BLE001 - any failure to validate is a refusal
            raise HTTPException(status_code=401, detail=f"workload identity not accepted: {type(e).__name__}") from e
        _validated.put(token, who, expiry)
        return who
    try:
        svid = _workload_api().validate_jwt_svid(token, AUDIENCE)
    except Exception as e:  # noqa: BLE001 - any failure to validate is a refusal
        raise HTTPException(status_code=401, detail=f"workload identity not accepted: {type(e).__name__}") from e
    spiffe_id = str(svid.spiffe_id)
    _validated.put(token, spiffe_id, float(svid.expiry))
    return spiffe_id


def _require(scope: str):
    key = WRITE_KEY if scope == "write" else LOOKUP_KEY

    def check(x_api_key: str = Header(default=""), authorization: str = Header(default="")) -> str:
        allowed = WRITERS if scope == "write" else READERS
        if MODE in ("spiffe", "entra"):
            if authorization.startswith("Bearer "):
                spiffe_id = caller(authorization.removeprefix("Bearer ").strip())
                if spiffe_id not in allowed:
                    raise HTTPException(status_code=403, detail=f"{spiffe_id} may not {scope} the ledger")
                return spiffe_id
            if scope == "read" and LOOKUP_KEY and x_api_key and secrets.compare_digest(x_api_key, LOOKUP_KEY):
                return "read key"   # the console, until it has a workload identity
            raise HTTPException(status_code=401, detail=(
                "the ledger takes writes from workload identities only" if scope == "write"
                else "send a workload identity, or the read key"))
        if not (key and x_api_key and secrets.compare_digest(x_api_key, key)):
            raise HTTPException(status_code=401, detail="invalid API key")
        return f"{scope} key"
    return check


require_write = _require("write")
require_read = _require("read")
