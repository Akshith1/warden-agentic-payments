"""HTTP client for warden-ledger. With LEDGER_AUTH=keys, two keys on purpose:
the read-only lookup key is used by Risk Scoring; the write key is used only
by Payment Execution. Confirming a vendor alias is a write to the vendor
master, so it uses the write key and happens only when a person approves.

Phase 6b, LEDGER_AUTH=spiffe: no keys. Every call carries this container's
SPIRE-issued identity, and the ledger decides what that identity may do. Which
agent role may call which method stays enforced in-process by the capability
manifests (capability.py)."""
import httpx

from . import config
from .workload_identity import Identity


class LedgerClient:
    def __init__(self, base_url: str = config.LEDGER_URL, timeout: float = 10.0):
        self._http = httpx.Client(base_url=base_url, timeout=timeout)
        self._identity = Identity()

    def _read(self) -> dict:
        return self._identity.headers(config.VENDOR_LOOKUP_KEY)

    def _write(self) -> dict:
        return self._identity.headers(config.LEDGER_WRITE_KEY)

    def lookup_vendor(self, name: str) -> dict:
        r = self._http.get("/vendors/lookup", params={"name": name},
                           headers=self._read())
        r.raise_for_status()
        return r.json()

    def dry_run(self, txn: dict) -> dict:
        r = self._http.post("/transactions/dry-run", json=txn, headers=self._write())
        r.raise_for_status()
        return r.json()

    def commit(self, txn: dict) -> tuple[bool, dict | list]:
        r = self._http.post("/transactions", json=txn, headers=self._write())
        if r.status_code in (409, 422):
            return False, r.json()["detail"]
        r.raise_for_status()
        return True, r.json()

    def record_payment(self, payment: dict) -> dict:
        r = self._http.post("/vendors/record-payment", json=payment,
                            headers=self._write())
        r.raise_for_status()
        return r.json()

    def confirm_alias(self, alias: str, vendor_id: int, confirmed_by: str, run_ref: str) -> tuple[bool, dict | str]:
        r = self._http.post("/vendors/aliases", headers=self._write(),
                            json={"alias": alias, "vendor_id": vendor_id, "confirmed_by": confirmed_by,
                                  "run_ref": run_ref})
        if r.status_code in (404, 409, 422):
            return False, r.json()["detail"]
        r.raise_for_status()
        return True, r.json()
