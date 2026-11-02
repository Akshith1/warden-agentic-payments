// Reading vendor history: the risk-scoring agent and people only.
@id("risk-scoring-reads-vendors")
@summary("The risk agent may read a vendor's payment history")
permit (
    principal == Warden::Agent::"risk-scoring",
    action == Warden::Action::"LookupVendor",
    resource is Warden::Vendor
);

@id("people-read-vendors")
@summary("A person may read a vendor's payment history")
permit (
    principal is Warden::User,
    action == Warden::Action::"LookupVendor",
    resource is Warden::Vendor
);
