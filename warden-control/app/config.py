import os

CONTROL_DB = os.environ.get("CONTROL_DB", "/data/control.db")
# Phase 11: the same check works for Entra ID on Azure; OIDC_* override the Keycloak names.
KEYCLOAK_ISSUER = os.environ.get("OIDC_ISSUER") or os.environ.get("KEYCLOAK_ISSUER", "http://localhost:8180/realms/warden")
KEYCLOAK_JWKS_URL = os.environ.get("OIDC_JWKS_URL") or os.environ.get(
    "KEYCLOAK_JWKS_URL", "http://keycloak:8080/realms/warden/protocol/openid-connect/certs")
TOKEN_AUDIENCE = os.environ.get("TOKEN_AUDIENCE", "warden-backend")
# Where a token lists the person's roles: Keycloak's realm_access.roles, or Entra ID's roles.
ROLES_CLAIM = os.environ.get("ROLES_CLAIM", "realm_access.roles")

# Automatic trips. A canary is a vendor name no genuine invoice contains; seeing one means
# someone is probing the pipeline, so everything stops. An agent refused this many times in
# this many minutes is paused on its own.
CANARY_VENDORS = [v.strip().upper() for v in os.environ.get("CANARY_VENDORS", "CANARY-DO-NOT-PAY").split(",")
                  if v.strip()]
DENIAL_LIMIT = int(os.environ.get("DENIAL_LIMIT", "5"))
DENIAL_WINDOW_MINUTES = int(os.environ.get("DENIAL_WINDOW_MINUTES", "10"))

# Phase 8: the gateway's per-agent rules, shown next to the spend (same file the gateway reads).
AGENTS_FILE = os.environ.get("WARDEN_AGENTS_FILE", "/etc/warden/agents.yaml")
