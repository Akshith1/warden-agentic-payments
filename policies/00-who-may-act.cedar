// Cedar denies anything no policy permits. These are the only permits for
// approving a payment and linking vendors; the forbids in 01-05 carve out
// what an agent may not approve on its own.
//
// Every policy carries an @id, every permit a @summary and every forbid a
// @reason, which the console shows to the person the run is escalated to.
// The validator rejects a policy without them.

@id("approval-agent-may-approve")
@summary("The approval agent may approve a payment, unless a rule below refuses it")
permit (
    principal == Warden::Agent::"approval",
    action == Warden::Action::"ApprovePayment",
    resource is Warden::Payment
);

// People approve by role, from their Keycloak login (phase 6). The backend
// verifies the token and passes the roles; it can't add roles of its own.
@id("approvers-may-approve")
@summary("A person with the approver role may approve up to 50,000.00")
permit (
    principal is Warden::User,
    action == Warden::Action::"ApprovePayment",
    resource is Warden::Payment
)
when { principal.roles.contains("approver") && resource.amountCents <= 5000000 };

@id("controllers-may-approve-any-amount")
@summary("A person with the controller role may approve any amount")
permit (
    principal is Warden::User,
    action == Warden::Action::"ApprovePayment",
    resource is Warden::Payment
)
when { principal.roles.contains("controller") };

@id("approvers-may-link-vendors")
@summary("A person with the approver role may confirm that two vendor names are the same business")
permit (
    principal is Warden::User,
    action == Warden::Action::"ConfirmVendorAlias",
    resource is Warden::Vendor
)
when { principal.roles.contains("approver") };
