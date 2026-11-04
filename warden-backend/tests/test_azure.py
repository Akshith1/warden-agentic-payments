"""Phase 11, the Azure profile: people signing in with Entra ID, the backend proving
itself to the ledger with a managed identity, and sleeping services woken before a run."""
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app import auth, config, wake, workload_identity

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
TENANT, APP = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"


class Keys:
    def get_signing_key_from_jwt(self, token):
        return type("K", (), {"key": KEY.public_key()})()


@pytest.fixture()
def entra(monkeypatch):
    monkeypatch.setattr(auth, "_jwks", Keys())
    monkeypatch.setattr(config, "KEYCLOAK_ISSUER", ISSUER)
    monkeypatch.setattr(config, "TOKEN_AUDIENCE", APP)
    monkeypatch.setattr(config, "ROLES_CLAIM", "roles")


def entra_token(**over):
    """Shaped like an Entra ID v2 access token for the Warden app, for a person."""
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": APP, "tid": TENANT, "sub": "pairwise-subject", "oid": "person-oid",
              "exp": now + 300, "iat": now, "preferred_username": "amira@warden.example", "name": "Amira Approver",
              "roles": ["approver", "submitter"], "scp": "access", "ver": "2.0"} | over
    return jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": "k1"})


def test_an_entra_token_names_the_person_and_their_warden_roles(entra):
    user = auth.verify(entra_token())
    assert user.username == "amira@warden.example" and user.name == "Amira Approver"
    assert user.roles == {"approver", "submitter"}


def test_roles_not_from_warden_are_ignored(entra):
    assert auth.verify(entra_token(roles=["approver", "Global Administrator"])).roles == {"approver"}


def test_an_entra_token_for_another_app_is_refused(entra):
    with pytest.raises(Exception) as e:
        auth.verify(entra_token(aud="33333333-3333-3333-3333-333333333333"))
    assert e.value.status_code == 401


def test_a_person_with_no_role_has_none(entra):
    assert auth.verify(entra_token(roles=None)).roles == frozenset()


def test_keycloak_roles_still_work_by_default():
    assert config.ROLES_CLAIM == "realm_access.roles"
    assert auth.roles_in({"realm_access": {"roles": ["controller"]}}) == ["controller"]
    assert auth.roles_in({"roles": ["controller"]}) == []


# ---- managed identity (the ledger's caller side)

def identity_endpoint(calls, expires_in=3600):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        token = f"mi-token-{len(calls)}"
        return httpx.Response(200, json={"access_token": token, "expires_on": str(int(time.time()) + expires_in),
                                         "resource": request.url.params["resource"], "token_type": "Bearer"})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_a_managed_identity_token_is_asked_for_the_ledger_and_reused(monkeypatch):
    monkeypatch.setenv("IDENTITY_ENDPOINT", "http://localhost:42356/msi/token")
    monkeypatch.setenv("IDENTITY_HEADER", "platform-secret")
    monkeypatch.setattr(workload_identity, "MODE", "entra")
    calls = []
    mi = workload_identity.ManagedIdentity(client_id="backend-client-id", http=identity_endpoint(calls))
    identity = workload_identity.Identity(audience="api://ledger-app", managed_identity=mi)
    assert identity.headers("ignored-key") == {"Authorization": "Bearer mi-token-1"}
    assert identity.headers("ignored-key") == {"Authorization": "Bearer mi-token-1"}   # cached
    sent = calls[0]
    assert sent.headers["X-IDENTITY-HEADER"] == "platform-secret"
    assert dict(sent.url.params) == {"resource": "api://ledger-app", "api-version": "2019-08-01",
                                     "client_id": "backend-client-id"}
    identity._expiry = time.time() + 30                                                # within a minute of expiry
    assert identity.token() == "mi-token-2"


# ---- waking sleeping services

def test_wake_asks_every_dependency_once_then_trusts_it_for_a_while(monkeypatch):
    monkeypatch.setattr(wake, "_last", None)
    asked = []
    result = wake.wake(get=asked.append)
    assert set(result) == {"ledger", "policy", "control", "pii", "gateway"} and set(result.values()) == {"up"}
    assert f"{config.CONTROL_URL}/health" in asked and f"{config.LITELLM_URL}/health/liveliness" in asked
    assert wake.wake(get=asked.append) == {} and len(asked) == 5                        # fresh: skipped


def test_wake_reports_a_dependency_that_stays_down_and_tries_again_next_time(monkeypatch):
    monkeypatch.setattr(wake, "_last", None)

    def get(url):
        if "/health/liveliness" in url:
            raise httpx.ConnectError("refused")
    result = wake.wake(get=get)
    assert result["gateway"].startswith("ConnectError") and result["ledger"] == "up"
    assert wake.wake(get=lambda url: None)["gateway"] == "up"                           # not marked fresh


def test_wake_is_off_unless_configured(monkeypatch):
    monkeypatch.setattr(wake, "_last", None)
    monkeypatch.setattr(wake, "ENABLED", False)
    assert wake.wake() == {}
