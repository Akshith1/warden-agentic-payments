"""Capability manifests: the second, independent lock (scenarios 09 and 10).

Each role has a manifest in app/capabilities/ listing the ledger actions it may
take. Nodes reach the ledger only through `GuardedLedger`, which checks the
manifest in plain code before any HTTP call. The policy service checks the
same actions with Cedar; either one saying no is enough. A policy mistake alone
can't give an agent the ledger, and neither can a code mistake alone."""
import json
from pathlib import Path

MANIFESTS = Path(__file__).resolve().parent / "capabilities"


class CapabilityDenied(PermissionError):
    pass


def manifest(agent: str) -> dict:
    path = MANIFESTS / f"{agent}.json"
    if not path.is_file():
        raise CapabilityDenied(f"{agent} has no capability manifest")
    return json.loads(path.read_text())


def require_capability(agent: str, action: str) -> None:
    if action not in manifest(agent)["allowed_actions"]:
        raise CapabilityDenied(f"{agent} may not {action} (not in its capability manifest)")


class GuardedLedger:
    """The ledger client as one role sees it."""
    ACTIONS = {"lookup_vendor": "LookupVendor", "dry_run": "DryRunLedgerEntry", "commit": "CreateLedgerEntry",
               "record_payment": "RecordVendorPayment", "confirm_alias": "ConfirmVendorAlias"}

    def __init__(self, ledger, agent: str):
        self._ledger, self.agent = ledger, agent

    def __getattr__(self, name):
        action = self.ACTIONS.get(name)
        if action is None:
            raise AttributeError(name)
        require_capability(self.agent, action)
        return getattr(self._ledger, name)


if __name__ == "__main__":  # scenarios 09 and 10: python -m app.capability
    for agent, action in [("extraction", "CreateLedgerEntry"), ("risk-scoring", "CreateLedgerEntry"),
                          ("approval", "CreateLedgerEntry"), ("critic", "CreateLedgerEntry"),
                          ("extraction", "LookupVendor"), ("risk-scoring", "LookupVendor"),
                          ("payment-execution", "CreateLedgerEntry")]:
        try:
            require_capability(agent, action)
            print(f"{agent:<18} {action:<18} ALLOWED")
        except CapabilityDenied as e:
            print(f"{agent:<18} {action:<18} DENIED: {e}")
