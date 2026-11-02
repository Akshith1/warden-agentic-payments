"""The three OpenMetadata API calls the job needs before it can ingest anything,
checked against the 2.0.7 server source (UserResource, BotResource):

    POST /api/v1/users/login                    email + base64 password -> accessToken
    GET  /api/v1/bots/name/ingestion-bot        -> botUser.id
    GET  /api/v1/users/auth-mechanism/{id}      -> config.JWTToken (admin only)

Ingestion then runs as the ingestion bot, not as the admin."""
import base64
import time

import requests

TIMEOUT = 15


class CatalogueError(RuntimeError):
    pass


def wait_until_up(server: str, seconds: float = 300) -> None:
    deadline = time.monotonic() + seconds
    while True:
        try:
            if requests.get(f"{server}/api/v1/system/version", timeout=5).ok:
                return
        except requests.RequestException:
            pass
        if time.monotonic() > deadline:
            raise CatalogueError(f"the catalogue at {server} did not answer within {seconds:.0f} s")
        time.sleep(5)


def admin_token(server: str, email: str, password: str) -> str:
    if not password:
        raise CatalogueError("OPENMETADATA_ADMIN_PASSWORD is empty: run infra/scripts/fill-secrets.py")
    r = requests.post(f"{server}/api/v1/users/login", timeout=TIMEOUT,
                      json={"email": email, "password": base64.b64encode(password.encode()).decode()})
    if r.status_code in (400, 401):
        raise CatalogueError("the catalogue refused the admin password. It is set only when the catalogue's "
                             "database is first created; see the README (Data catalogue) to reset it.")
    r.raise_for_status()
    return r.json()["accessToken"]


def bot_token(server: str, admin: str, bot: str = "ingestion-bot") -> str:
    headers = {"Authorization": f"Bearer {admin}"}
    r = requests.get(f"{server}/api/v1/bots/name/{bot}", params={"fields": "botUser"}, headers=headers,
                     timeout=TIMEOUT)
    r.raise_for_status()
    user_id = r.json()["botUser"]["id"]
    r = requests.get(f"{server}/api/v1/users/auth-mechanism/{user_id}", headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    token = (r.json().get("config") or {}).get("JWTToken")
    if not token:
        raise CatalogueError(f"the {bot} has no token yet; open Settings > Bots in the catalogue once")
    return token
