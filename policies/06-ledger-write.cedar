// Only the payment-execution agent may write to the ledger. Trust tiers come
// from the policy service's own agent registry, not from the caller. The
// backend's capability manifests are the second, independent lock: either
// one denying is enough to stop a write.
@id("only-write-tier-writes-ledger")
@reason("only the payment-execution agent may write to the ledger")
@summary("No other agent may write to the ledger, whatever else permits it")
forbid (
    principal is Warden::Agent,
    action in [
        Warden::Action::"DryRunLedgerEntry",
        Warden::Action::"CreateLedgerEntry",
        Warden::Action::"RecordVendorPayment"
    ],
    resource is Warden::Payment
)
unless { principal.trustTier == "write-ledger" };

@id("payment-execution-writes-ledger")
@summary("The payment-execution agent may dry-run and post ledger entries")
permit (
    principal == Warden::Agent::"payment-execution",
    action in [
        Warden::Action::"DryRunLedgerEntry",
        Warden::Action::"CreateLedgerEntry",
        Warden::Action::"RecordVendorPayment"
    ],
    resource is Warden::Payment
)
when { principal.trustTier == "write-ledger" };
