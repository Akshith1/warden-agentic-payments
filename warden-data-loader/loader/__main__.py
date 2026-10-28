"""Re-runnable: every insert is keyed on a natural external key, so a second
run adds nothing it already has. Phase 7 adds the Great Expectations checkpoint here."""
from . import invoices, vendors

if __name__ == "__main__":
    vendors.run()
    invoices.run()
