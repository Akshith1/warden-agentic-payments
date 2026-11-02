"""Submits real invoices from invoice_inbox to the agent pipeline.

    python -m loader.submit --count 3              # first 3 SROIE test receipts
    python -m loader.submit --vendor "SANYU"       # receipts whose label matches a vendor
    python -m loader.submit --key X00016469612     # one specific document

Since phase 6 the backend needs a sign-in: the loader signs in as WARDEN_USERNAME
(asking for the password if WARDEN_PASSWORD isn't set) through Keycloak's warden-cli
client, and submits under that person's name.
"""
import argparse
import getpass
import os

import httpx

from . import db

BACKEND_URL = os.environ.get("BACKEND_URL", "http://warden-backend:8000")
TOKEN_URL = os.environ.get("KEYCLOAK_TOKEN_URL", "http://keycloak:8080/realms/warden/protocol/openid-connect/token")


def sign_in() -> str:
    user = os.environ.get("WARDEN_USERNAME") or input("Warden username: ")
    password = os.environ.get("WARDEN_PASSWORD") or getpass.getpass(f"Password for {user}: ")
    r = httpx.post(TOKEN_URL, data={"grant_type": "password", "client_id": "warden-cli", "username": user,
                                    "password": password, "scope": "openid"}, timeout=15)
    if r.status_code != 200:
        raise SystemExit(f"Keycloak refused the sign-in: {r.json().get('error_description', r.text)}"
                         " (sign in once in the console first, to replace the temporary password)")
    return r.json()["access_token"]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--count", type=int, default=1)
    p.add_argument("--source", choices=["sroie", "cord"], default="sroie")
    p.add_argument("--split", default="test")
    p.add_argument("--vendor", help="case-insensitive match on the ground-truth company name (SROIE)")
    p.add_argument("--key", help="one external_key")
    args = p.parse_args()

    with db.backend_conn() as conn:
        if args.key:
            rows = conn.execute("SELECT external_key, text FROM invoice_inbox WHERE external_key = %s",
                                (args.key,)).fetchall()
        else:
            rows = conn.execute(
                """SELECT external_key, text FROM invoice_inbox
                   WHERE source = %s AND split = %s
                     AND (%s::text IS NULL OR ground_truth->'entities'->>'company' ILIKE '%%' || %s || '%%')
                   ORDER BY id LIMIT %s""",
                (args.source, args.split, args.vendor, args.vendor, args.count),
            ).fetchall()

    headers = {"Authorization": f"Bearer {sign_in()}"} if rows else {}
    for row in rows:
        r = httpx.post(f"{BACKEND_URL}/runs", json={"invoice_text": row["text"], "source": "dataset"},
                       headers=headers, timeout=30)
        r.raise_for_status()
        print(f"{row['external_key']} -> run {r.json()['run_id']}")
    if not rows:
        print("no matching documents in invoice_inbox; run the loader first")


if __name__ == "__main__":
    main()
