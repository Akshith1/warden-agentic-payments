// The ledger is single-currency and never converts.
@id("agents-ledger-currency-only")
@reason("not in the ledger currency (MYR), so a person must approve")
forbid (
    principal is Warden::Agent,
    action == Warden::Action::"ApprovePayment",
    resource is Warden::Payment
)
when { resource.currency != "MYR" };
