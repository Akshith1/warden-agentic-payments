"""Token checks with a real RSA key and real JWTs; only the key download is replaced."""
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import auth, config, main

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class Keys:
    def get_signing_key_from_jwt(self, token):
        return type("K", (), {"key": KEY.public_key()})()


def token(key=KEY, **over):
    now = int(time.time())
    claims = {"iss": config.KEYCLOAK_ISSUER, "aud": ["warden-backend", "account"], "sub": "u-1",
              "exp": now + 300, "iat": now, "preferred_username": "rajesh", "name": "Rajesh Vasa",
              "realm_access": {"roles": ["approver", "default-roles-warden", "offline_access"]}} | over
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setattr(auth, "_jwks", Keys())


def test_a_good_token_names_the_person_and_their_warden_roles():
    user = auth.verify(token())
    assert user.username == "rajesh" and user.name == "Rajesh Vasa" and user.roles == {"approver"}


@pytest.mark.parametrize("bad", [
    pytest.param(lambda: token(key=OTHER), id="signed by another key"),
    pytest.param(lambda: token(iss="http://evil.example/realms/warden"), id="another issuer"),
    pytest.param(lambda: token(aud="account"), id="not meant for the backend"),
    pytest.param(lambda: token(exp=int(time.time()) - 120), id="expired"),
])
def test_bad_tokens_are_refused(bad):
    with pytest.raises(HTTPException) as e:
        auth.verify(bad())
    assert e.value.status_code == 401


def test_endpoints_need_a_token_and_the_right_role(monkeypatch):
    monkeypatch.setattr(main.config, "MLFLOW_TRACKING_URI", "")
    with TestClient(main.app) as api:
        assert api.get("/health").status_code == 200  # the only open endpoint
        assert api.get("/runs").status_code == 401
        assert api.post("/runs", json={"invoice_text": "x"}).status_code == 401
        approver_only = {"Authorization": f"Bearer {token()}"}
        assert api.get("/me", headers=approver_only).json()["roles"] == ["approver"]
        r = api.post("/runs", json={"invoice_text": "x"}, headers=approver_only)
        assert r.status_code == 403 and r.json()["detail"] == "needs the submitter role"
        no_roles = {"Authorization": f"Bearer {token(realm_access={'roles': ['offline_access']})}"}
        assert api.get("/runs", headers=no_roles).status_code == 403
        assert api.get("/runs", headers={"Authorization": "Bearer not-a-jwt"}).status_code == 401


def test_each_agent_calls_with_its_own_gateway_key(tmp_path, monkeypatch):
    import json

    from app import llm
    keys = tmp_path / "agent-keys.json"
    keys.write_text(json.dumps({"extraction": "sk-ex", "critic": "sk-cr"}))
    monkeypatch.setattr(config, "AGENT_KEYS_FILE", str(keys))
    monkeypatch.setattr(config, "LITELLM_API_KEY", "")
    llm._agent_keys.cache_clear()
    assert llm.key_for("extraction") == "sk-ex" and llm.key_for("critic") == "sk-cr"
    with pytest.raises(RuntimeError, match="no gateway key for the approval agent"):
        llm.key_for("approval")
    llm._agent_keys.cache_clear()
