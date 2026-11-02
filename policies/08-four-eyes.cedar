// Separation of duties: the person who submitted an invoice can't approve it,
// whatever their role. A second person has to. submittedBy is the username
// from the submitter's verified login, recorded when the run was created.
@id("four-eyes")
@reason("you submitted this invoice, so another person must approve it")
@summary("Nobody may approve an invoice they submitted themselves")
forbid (
    principal is Warden::User,
    action == Warden::Action::"ApprovePayment",
    resource is Warden::Payment
)
when { resource.submittedBy == principal.username };
