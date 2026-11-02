"""Fills every empty secret in .env with a fresh random value; leaves set values alone.

    python infra/scripts/fill-secrets.py

Run it after copying .env.example to .env, and again after pulling a phase that adds a
setting: anything in .env.example that your .env doesn't have yet is added first (with
its default, or a fresh secret). Postgres passwords only take effect on a new database
volume; for an existing install see the README ("Rotating secrets")."""
import re
import secrets
from pathlib import Path

SECRETS = re.compile(r"^(POSTGRES_PASSWORD|PGPASS_[A-Z_]+|LITELLM_MASTER_KEY|LEDGER_WRITE_KEY|VENDOR_LOOKUP_KEY"
                     r"|KEYCLOAK_ADMIN_PASSWORD)=\s*$")

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
        value = ("sk-" if m.group(1) == "LITELLM_MASTER_KEY" else "") + secrets.token_urlsafe(32)
        line = f"{m.group(1)}={value}"
        filled.append(m.group(1))
    lines.append(line)
env.write_text("\n".join(lines) + "\n")
if added:
    print("Added from .env.example:", ", ".join(added))
print("Filled:", ", ".join(filled) if filled else "nothing (every secret already has a value)")
