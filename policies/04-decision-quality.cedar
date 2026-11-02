@id("agents-need-confidence")
@reason("the approval agent was less than 75% sure, so a person must approve")
forbid (
    principal is Warden::Agent,
    action == Warden::Action::"ApprovePayment",
    resource is Warden::Payment
)
when { resource.confidencePct < 75 };

@id("agents-need-critic")
@reason("the critic agent did not agree, so a person must approve")
forbid (
    principal is Warden::Agent,
    action == Warden::Action::"ApprovePayment",
    resource is Warden::Payment
)
when { !resource.criticAgrees };

@id("remit-change-needs-person")
@reason("the pay-to account differs from the vendor's last payment, so a person must approve")
forbid (
    principal is Warden::Agent,
    action == Warden::Action::"ApprovePayment",
    resource is Warden::Payment
)
when { resource.remitChanged };
