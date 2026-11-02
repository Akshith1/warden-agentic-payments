"""Fills every empty secret in .env with a fresh random value; leaves set values alone.

    python infra/scripts/fill-secrets.py

Run it after copying .env.example to .env, and again after pulling a phase that adds a
setting: anything in .env.example that your .env doesn't have yet is added first (with
its default, or a fresh secret). Postgres passwords only take effect on a new database
volume; for an existing install see the README ("Rotating secrets")."""
import base64
import re
import secrets
from pathlib import Path

SECRETS = re.compile(r"^(POSTGRES_PASSWORD|PGPASS_[A-Z_]+|LITELLM_MASTER_KEY|LEDGER_WRITE_KEY|VENDOR_LOOKUP_KEY"
                     r"|KEYCLOAK_ADMIN_PASSWORD|OPENMETADATA_ADMIN_PASSWORD|OPENMETADATA_FERNET_KEY|GRAFANA_ADMIN_PASSWORD)=\s*$")


def fernet_key() -> str:
    # 32 random bytes in base64. Drawn until the text has no + / - _, so it reads the same in
    # standard and URL-safe base64, whichever the catalogue's Java library decodes.
    while True:
        key = base64.b64encode(secrets.token_bytes(32)).decode()
        if not set(key) & set("+/-_"):
            return key


def value_for(name: str) -> str:
    if name == "LITELLM_MASTER_KEY":
        return "sk-" + secrets.token_urlsafe(32)
    if name == "OPENMETADATA_FERNET_KEY":
        return fernet_key()
    if name == "OPENMETADATA_ADMIN_PASSWORD":
        # The catalogue asks for upper and lower case, a digit and a symbol, 8 to 56 characters.
        return secrets.token_urlsafe(18) + "-Wd7"
    return secrets.token_urlsafe(32)

root = Path(__file__).resolve().parents[2]
env = root / ".env"
if not env.exists():
    raise SystemExit("No .env yet: copy .env.example to .env first.")
current = env.read_text().splitlines()
have = {line.split("=", 1)[0].strip() for line in current if "=" in line and not line.lstrip().startswith("#")}
added = []
for line in (root / ".env.example").read_text().splitlines():
    name = line.split("=", 1)[0].strip()
    if "=" in line and not line.lstrip().startswith("#") and name not in have:
        current.append(line)
        added.append(name)
lines, filled = [], []
for line in current:
    m = SECRETS.match(line)
    if m:
        value = value_for(m.group(1))
        line = f"{m.group(1)}={value}"
        filled.append(m.group(1))
    lines.append(line)
env.write_text("\n".join(lines) + "\n")
if added:
    print("Added from .env.example:", ", ".join(added))
print("Filled:", ", ".join(filled) if filled else "nothing (every secret already has a value)")
