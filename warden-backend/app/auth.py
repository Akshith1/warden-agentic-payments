"""Who is calling (phase 6). Every endpoint except /health needs a Keycloak
access token for the warden realm, sent as `Authorization: Bearer <token>`.

The token is checked here: signature against Keycloak's published keys,
issuer, audience (warden-backend), expiry. The person's username and roles
come only from a token that passes; nothing the caller sends in the request
body can name the approver any more."""
from dataclasses import dataclass, field

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import config

ROLES = {"submitter", "approver", "controller"}
_bearer = HTTPBearer(auto_error=False)
_jwks: jwt.PyJWKClient | None = None


@dataclass(frozen=True)
class User:
    username: str
    name: str
    roles: frozenset[str] = field(default_factory=frozenset)

    def has(self, *roles: str) -> bool:
        return bool(self.roles & set(roles))


def _keys() -> jwt.PyJWKClient:
    global _jwks
    if _jwks is None:
        _jwks = jwt.PyJWKClient(config.KEYCLOAK_JWKS_URL, cache_keys=True, lifespan=300, timeout=10)
    return _jwks


def roles_in(claims: dict) -> list[str]:
    """The roles at config.ROLES_CLAIM, a dotted path: realm_access.roles (Keycloak) or roles (Entra ID)."""
    value = claims
    for part in config.ROLES_CLAIM.split("."):
        value = value.get(part, {}) if isinstance(value, dict) else {}
    return [r for r in value if isinstance(r, str)] if isinstance(value, list) else []


def verify(token: str) -> User:
    try:
        key = _keys().get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=["RS256"], audience=config.TOKEN_AUDIENCE,
                            issuer=config.KEYCLOAK_ISSUER, options={"require": ["exp", "iss", "aud", "sub"]},
                            leeway=30)
    except jwt.PyJWKClientConnectionError as e:
        raise HTTPException(status_code=503, detail="can't reach the sign-in service to check the sign-in") from e
    except jwt.PyJWTError as e:
        raise HTTPException(status_code=401, detail=f"sign-in not accepted: {e}",
                            headers={"WWW-Authenticate": "Bearer"}) from e
    roles = frozenset(roles_in(claims)) & ROLES
    username = claims.get("preferred_username") or claims["sub"]
    return User(username=username, name=claims.get("name") or username, roles=roles)


def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> User:
    if credentials is None:
        raise HTTPException(status_code=401, detail="sign in first", headers={"WWW-Authenticate": "Bearer"})
    return verify(credentials.credentials)


def require(*roles: str):
    """A dependency that lets in only people holding at least one of `roles`."""
    def check(user: User = Depends(current_user)) -> User:
        if roles and not user.has(*roles):
            raise HTTPException(status_code=403, detail=f"needs the {' or '.join(roles)} role")
        return user
    return check
