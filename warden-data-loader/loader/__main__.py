"""Re-runnable: every insert is keyed on a natural external key, so a second
run adds nothing it already has. After loading, the data-quality service
(phase 7b) checks what was loaded; a failed critical check stops payments."""
import os

import httpx

from . import invoices, vendors

QUALITY_URL = os.environ.get("QUALITY_URL", "http://warden-quality:8000")


def check_quality() -> None:
    try:
        result = httpx.post(f"{QUALITY_URL}/run", timeout=180).raise_for_status().json()
    except httpx.HTTPError as e:
        print(f"Data-quality checks not run ({e}). They also run every 10 minutes, or from Governance.")
        return
    print(f"Data-quality checks: {result['passed']} passed, {result['failed']} failed, {result['errors']} errors")
    for check in result["checks"]:
        if check["status"] != "passed":
            detail = check.get("error") or f"{check['count']} row(s)"
            print(f"  {check['status'].upper():7} {check['severity']:8} {check['title']}: {detail}")
    if result["critical_failed"]:
        raise SystemExit("A critical data-quality check failed: payments are stopped until a controller resumes.")


if __name__ == "__main__":
    vendors.run()
    invoices.run()
    check_quality()
