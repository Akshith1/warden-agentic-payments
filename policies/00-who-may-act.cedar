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

// Any person may approve for now. Phase 6 (Keycloak) adds approval tiers,
// such as a controller for amounts above a second limit.
@id("people-may-approve")
@summary("A person may approve any payment the agents could not")
permit (
    principal is Warden::User,
    action == Warden::Action::"ApprovePayment",
    resource is Warden::Payment
);

@id("people-may-link-vendors")
@summary("A person may confirm that two vendor names are the same business")
permit (
    principal is Warden::User,
    action == Warden::Action::"ConfirmVendorAlias",
    resource is Warden::Vendor
);
