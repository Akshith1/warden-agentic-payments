"""Fills every empty secret in .env with a fresh random value; leaves set values alone.

    python infra/scripts/fill-secrets.py

Run it after copying .env.example to .env. Postgres passwords only take effect on a
new database volume; for an existing install see the README ("Rotating secrets")."""
import re
import secrets
from pathlib import Path

SECRETS = re.compile(r"^(POSTGRES_PASSWORD|PGPASS_[A-Z_]+|LITELLM_MASTER_KEY|LEDGER_WRITE_KEY|VENDOR_LOOKUP_KEY"
                     r"|KEYCLOAK_ADMIN_PASSWORD)=\s*$")

env = Path(__file__).resolve().parents[2] / ".env"
if not env.exists():
    raise SystemExit("No .env yet: copy .env.example to .env first.")
lines, filled = [], []
for line in env.read_text().splitlines():
    m = SECRETS.match(line)
    if m:
        value = ("sk-" if m.group(1) == "LITELLM_MASTER_KEY" else "") + secrets.token_urlsafe(32)
        line = f"{m.group(1)}={value}"
        filled.append(m.group(1))
    lines.append(line)
env.write_text("\n".join(lines) + "\n")
print("Filled:", ", ".join(filled) if filled else "nothing (every secret already has a value)")
