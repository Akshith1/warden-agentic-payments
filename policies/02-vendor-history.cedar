// Trust is earned from the ledger: vendorKnown and priorPayments are looked up
// by the policy service itself, never taken from the caller.
@id("agents-need-vendor-history")
@reason("the vendor has fewer than 2 settled payments, so a person must approve")
forbid (
    principal is Warden::Agent,
    action == Warden::Action::"ApprovePayment",
    resource is Warden::Payment
)
when { !resource.vendorKnown || resource.priorPayments < 2 };
