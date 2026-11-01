"""HTTP client for warden-ledger. Two keys on purpose: the read-only lookup
key is used by Risk Scoring; the write key is used only by Payment Execution.
Confirming a vendor alias is a write to the vendor master, so it uses the
write key and happens only when a person approves. Phase 6 moves the write key into Infisical, fetched fresh per call."""
import httpx

from . import config


class LedgerClient:
    def __init__(self, base_url: str = config.LEDGER_URL, timeout: float = 10.0):
        self._http = httpx.Client(base_url=base_url, timeout=timeout)

    def lookup_vendor(self, name: str) -> dict:
        r = self._http.get("/vendors/lookup", params={"name": name},
                           headers={"X-API-Key": config.VENDOR_LOOKUP_KEY})
        r.raise_for_status()
        return r.json()

    def dry_run(self, txn: dict) -> dict:
        r = self._http.post("/transactions/dry-run", json=txn, headers={"X-API-Key": config.LEDGER_WRITE_KEY})
        r.raise_for_status()
        return r.json()

    def commit(self, txn: dict) -> tuple[bool, dict | list]:
        r = self._http.post("/transactions", json=txn, headers={"X-API-Key": config.LEDGER_WRITE_KEY})
        if r.status_code in (409, 422):
            return False, r.json()["detail"]
        r.raise_for_status()
        return True, r.json()

    def record_payment(self, payment: dict) -> dict:
        r = self._http.post("/vendors/record-payment", json=payment,
                            headers={"X-API-Key": config.LEDGER_WRITE_KEY})
        r.raise_for_status()
        return r.json()

    def confirm_alias(self, alias: str, vendor_id: int, confirmed_by: str, run_ref: str) -> tuple[bool, dict | str]:
        r = self._http.post("/vendors/aliases", headers={"X-API-Key": config.LEDGER_WRITE_KEY},
                            json={"alias": alias, "vendor_id": vendor_id, "confirmed_by": confirmed_by,
                                  "run_ref": run_ref})
        if r.status_code in (404, 409, 422):
            return False, r.json()["detail"]
        r.raise_for_status()
        return True, r.json()
