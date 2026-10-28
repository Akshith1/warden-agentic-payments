"""Seeds the ledger's vendor master and payment history from USAspending.gov
contract awards: real recipient names, amounts and dates."""
import os
from datetime import date

import httpx

from . import db

API_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
AGENCY = os.environ.get("USASPENDING_AGENCY", "Department of Health and Human Services")
FISCAL_YEAR = int(os.environ.get("USASPENDING_FISCAL_YEAR", "2025"))
PAGES = int(os.environ.get("USASPENDING_PAGES", "3"))
FIELDS = ["Award ID", "Recipient Name", "Start Date", "Award Amount", "generated_internal_id"]


def fetch_awards() -> list[dict]:
    awards = []
    with httpx.Client(timeout=60) as http:
        for page in range(1, PAGES + 1):
            r = http.post(API_URL, json={
                "filters": {
                    "award_type_codes": ["A", "B", "C", "D"],
                    "time_period": [{"start_date": f"{FISCAL_YEAR - 1}-10-01", "end_date": f"{FISCAL_YEAR}-09-30"}],
                    "agencies": [{"type": "awarding", "tier": "toptier", "name": AGENCY}],
                },
                "fields": FIELDS, "page": page, "limit": 100, "sort": "Award Amount", "order": "desc",
            })
            r.raise_for_status()
            body = r.json()
            awards.extend(body.get("results", []))
            if not body.get("page_metadata", {}).get("hasNext"):
                break
    return awards


def run() -> dict:
    awards = fetch_awards()
    seeded = skipped = 0
    with db.ledger_conn() as conn:
        for a in awards:
            name, amount, ref = a.get("Recipient Name"), a.get("Award Amount"), a.get("generated_internal_id")
            if not name or not ref or amount is None or amount <= 0 or not a.get("Start Date"):
                skipped += 1  # negative/zero amounts are de-obligations, not payments
                continue
            start = date.fromisoformat(a["Start Date"])
            vendor_id = conn.execute(
                """INSERT INTO vendors (normalized_name, display_name, first_seen) VALUES (%s,%s,%s)
                   ON CONFLICT (normalized_name)
                   DO UPDATE SET first_seen = LEAST(vendors.first_seen, EXCLUDED.first_seen)
                   RETURNING id""",
                (db.normalize_vendor(name), name.strip(), start),
            ).fetchone()["id"]
            seeded += conn.execute(
                """INSERT INTO historical_payments (vendor_id, amount, paid_on, source, external_ref)
                   VALUES (%s,%s,%s,'usaspending',%s) ON CONFLICT (external_ref) DO NOTHING""",
                (vendor_id, round(amount, 2), start, f"usaspending:{ref}"),
            ).rowcount
        vendors = conn.execute("SELECT count(*) AS n FROM vendors").fetchone()["n"]
    print(f"USAspending ({AGENCY}, FY{FISCAL_YEAR}): {seeded} new awards, {skipped} skipped, "
          f"{vendors} vendors in the ledger")
    return {"awards": seeded, "skipped": skipped, "vendors": vendors}
