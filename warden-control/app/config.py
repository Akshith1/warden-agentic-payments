import os

CONTROL_DB = os.environ.get("CONTROL_DB", "/data/control.db")
KEYCLOAK_ISSUER = os.environ.get("KEYCLOAK_ISSUER", "http://localhost:8180/realms/warden")
KEYCLOAK_JWKS_URL = os.environ.get("KEYCLOAK_JWKS_URL",
                                   "http://keycloak:8080/realms/warden/protocol/openid-connect/certs")
TOKEN_AUDIENCE = os.environ.get("TOKEN_AUDIENCE", "warden-backend")

# Automatic trips. A canary is a vendor name no genuine invoice contains; seeing one means
# someone is probing the pipeline, so everything stops. An agent refused this many times in
# this many minutes is paused on its own.
CANARY_VENDORS = [v.strip().upper() for v in os.environ.get("CANARY_VENDORS", "CANARY-DO-NOT-PAY").split(",")
                  if v.strip()]
DENIAL_LIMIT = int(os.environ.get("DENIAL_LIMIT", "5"))
DENIAL_WINDOW_MINUTES = int(os.environ.get("DENIAL_WINDOW_MINUTES", "10"))
