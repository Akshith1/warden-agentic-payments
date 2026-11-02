// A rule over time. Cedar judges one request and remembers nothing, so the
// policy service supplies paymentsLast24h: the larger of the vendor's settled
// payments in the last 24 hours (from the ledger) and the agent approvals it
// allowed for that vendor in the same window (which catches a burst submitted
// together, before any of it settles). Split invoices or a compromised vendor
// account stop at the fourth payment and wait for a person.
@id("agents-vendor-rate-limit")
@reason("the vendor already had 3 payments approved in the last 24 hours, so a person must approve")
forbid (
    principal is Warden::Agent,
    action == Warden::Action::"ApprovePayment",
    resource is Warden::Payment
)
when { resource.paymentsLast24h >= 3 };
