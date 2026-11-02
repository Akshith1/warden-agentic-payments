// Amounts are in cents: 500000 is 5,000.00 in the ledger currency.
@id("agents-routine-amounts-only")
@reason("above the 5,000.00 routine amount, so a person must approve")
forbid (
    principal is Warden::Agent,
    action == Warden::Action::"ApprovePayment",
    resource is Warden::Payment
)
when { resource.amountCents > 500000 };
