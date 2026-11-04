"""Phase 11: the ledger with LEDGER_AUTH=entra, as on Azure Container Apps. Tokens are
real RS256 JWTs shaped like the ones Entra ID issues to a managed identity (version 1
and 2); only the download of Entra's public keys is replaced."""
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from .test_ledger import R, W, client, pay  # noqa: F401, I001  (first: it sets the keys app.auth reads)

from app import auth  # noqa: E402

TENANT = "11111111-1111-1111-1111-111111111111"
LEDGER_APP = "22222222-2222-2222-2222-222222222222"
BACKEND_MI, POLICY_MI, OTHER_MI = ("aaaaaaaa-0000-0000-0000-00000000000b", "aaaaaaaa-0000-0000-0000-00000000000p",
                                   "aaaaaaaa-0000-0000-0000-00000000000x")
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class Keys:
    def get_signing_key_from_jwt(self, token):
        return type("K", (), {"key": KEY.public_key()})()


@pytest.fixture()
def entra(monkeypatch):
    monkeypatch.setattr(auth, "MODE", "entra")
    monkeypatch.setattr(auth, "TENANT", TENANT)
    monkeypatch.setattr(auth, "AUDIENCE", LEDGER_APP)
    monkeypatch.setattr(auth, "ENTRA_ISSUERS", frozenset({f"https://login.microsoftonline.com/{TENANT}/v2.0",
                                                          f"https://sts.windows.net/{TENANT}/"}))
    monkeypatch.setattr(auth, "WRITERS", frozenset({BACKEND_MI}))
    monkeypatch.setattr(auth, "READERS", frozenset({BACKEND_MI, POLICY_MI}))
    monkeypatch.setattr(auth, "_validated", auth._Validated())
    monkeypatch.setattr(auth, "_entra_keys", Keys())


def token(caller=BACKEND_MI, version=2, tenant=TENANT, audience=None, ttl=600, key=KEY):
    now = int(time.time())
    claims = {"tid": tenant, "iat": now, "nbf": now, "exp": now + ttl, "oid": caller, "sub": caller,
              "idtyp": "app"}
    if version == 2:
        claims |= {"iss": f"https://login.microsoftonline.com/{tenant}/v2.0", "aud": audience or LEDGER_APP,
                   "azp": caller, "ver": "2.0"}
    else:
        claims |= {"iss": f"https://sts.windows.net/{tenant}/", "aud": audience or f"api://{LEDGER_APP}",
                   "appid": caller, "ver": "1.0"}
    return {"Authorization": "Bearer " + jwt.encode(claims, key, algorithm="RS256")}


@pytest.mark.parametrize("version", [1, 2])
def test_the_backend_identity_writes(client, entra, version):
    r = client.post("/transactions/dry-run", json=pay(10), headers=token(version=version))
    assert r.status_code == 200 and r.json()["valid"] is True


def test_the_policy_identity_reads_but_may_not_write(client, entra):
    assert client.get("/vendors/lookup", params={"name": "Acme"}, headers=token(POLICY_MI)).status_code == 200
    r = client.post("/transactions", json=pay(10), headers=token(POLICY_MI))
    assert r.status_code == 403 and r.json()["detail"] == f"{POLICY_MI} may not write the ledger"


def test_another_identity_in_the_tenant_is_refused(client, entra):
    assert client.get("/accounts", headers=token(OTHER_MI)).status_code == 403


def test_the_write_key_stops_working(client, entra):
    r = client.post("/transactions", json=pay(10), headers=W)
    assert r.status_code == 401 and r.json()["detail"] == "the ledger takes writes from workload identities only"


def test_the_console_still_reads_with_the_read_key(client, entra):
    assert client.get("/accounts", headers=R).status_code == 200


@pytest.mark.parametrize("headers", [
    pytest.param(lambda: token(tenant="99999999-9999-9999-9999-999999999999"), id="another tenant"),
    pytest.param(lambda: token(audience="33333333-3333-3333-3333-333333333333"), id="meant for another app"),
    pytest.param(lambda: token(ttl=-120), id="expired"),
    pytest.param(lambda: token(key=OTHER_KEY), id="not signed by Entra"),
    pytest.param(lambda: {"Authorization": "Bearer not-a-token"}, id="not a token"),
])
def test_bad_tokens_are_refused(client, entra, headers):
    r = client.post("/transactions", json=pay(10), headers=headers())
    assert r.status_code == 401 and r.json()["detail"].startswith("workload identity not accepted")


def test_a_person_signed_in_to_warden_is_not_a_workload(client, entra):
    # A person's token names the console as azp, which is no ledger writer.
    r = client.post("/transactions", json=pay(10), headers=token("44444444-4444-4444-4444-444444444444"))
    assert r.status_code == 403
