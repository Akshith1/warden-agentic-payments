"""Loading and checking the Cedar policy set.

A policy set is accepted only if every file parses, the whole set validates
against the schema, no rule is wide open, every rule names itself (@id), every
permit says what it allows (@summary), every forbid says why (@reason), and the authorization matrix below gives the
expected answer for every case. The service refuses to start on a rejected
set, so a bad policy change fails closed instead of being served."""
import json
from dataclasses import dataclass, field
from pathlib import Path

import cedarpy

SCHEMA_FILE = "warden.cedarschema.json"
TRUST_TIERS = json.loads((Path(__file__).with_name("agents.json")).read_text())
TRUST_TIERS.pop("_comment", None)


@dataclass
class Rule:
    id: str
    effect: str
    reason: str | None
    summary: str | None
    file: str


@dataclass
class PolicySet:
    text: str
    schema: str
    rules: dict[str, Rule]  # Cedar's own id ("policy3") -> our rule
    files: list[str] = field(default_factory=list)

    def authorize(self, principal: dict, action: str, resource: dict) -> dict:
        entities = [principal_entity(principal), resource_entity(resource)]
        result = cedarpy.is_authorized(
            {"principal": uid(principal["type"], principal["id"]), "action": f'Warden::Action::"{action}"',
             "resource": uid(resource["type"], resource["id"]), "context": {}},
            self.text, entities, schema=self.schema)
        matched = [self.rules[p] for p in result.diagnostics.reasons if p in self.rules]
        errors = [str(e) for e in result.diagnostics.errors]
        allowed = result.decision == cedarpy.Decision.Allow and not errors
        denials = [r for r in matched if r.effect == "forbid"]
        if allowed:
            reasons = []
        elif denials:
            reasons = [r.reason for r in denials]
        elif errors:
            reasons = ["the policy engine could not evaluate the request"]
        else:
            reasons = [f"no policy permits {principal['id']} to {action}"]
        return {"decision": "Allow" if allowed else "Deny", "policies": [r.id for r in matched],
                "reasons": reasons, "errors": errors}


def uid(entity_type: str, entity_id: str) -> str:
    return f'Warden::{entity_type}::{json.dumps(entity_id)}'


def principal_entity(p: dict) -> dict:
    if p["type"] == "Agent":
        attrs = {"trustTier": TRUST_TIERS.get(p["id"], "none")}
    else:  # roles come from the person's verified login, checked by the backend
        attrs = {"username": p["id"], "roles": sorted(set(p.get("roles") or []))}
    return {"uid": {"type": f"Warden::{p['type']}", "id": p["id"]}, "attrs": attrs, "parents": []}


def resource_entity(r: dict) -> dict:
    return {"uid": {"type": f"Warden::{r['type']}", "id": r["id"]}, "attrs": r.get("attrs", {}), "parents": []}


class Rejected(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def load(directory: str | Path) -> PolicySet:
    directory = Path(directory)
    files = sorted(directory.glob("*.cedar"))
    schema_path = directory / SCHEMA_FILE
    problems = []
    if not files:
        raise Rejected([f"no .cedar files in {directory}"])
    if not schema_path.exists():
        raise Rejected([f"{SCHEMA_FILE} missing from {directory}"])

    rules_by_file = []
    for f in files:
        try:
            parsed = json.loads(cedarpy.policies_to_json_str(f.read_text()))["staticPolicies"]
        except ValueError as e:
            problems.append(f"{f.name}: does not parse: {e}")
            continue
        rules_by_file.append((f, parsed))
        for p in parsed.values():
            problems.extend(f"{f.name}: {msg}" for msg in rule_problems(p))
    if problems:
        raise Rejected(problems)

    text = "\n".join(f.read_text() for f in files)
    schema = schema_path.read_text()
    validation = cedarpy.validate_policies(text, schema)
    if not validation.validation_passed:
        raise Rejected([f"schema: {e}" for e in validation.errors])

    # Cedar numbers policies policy0, policy1, ... in the order of the concatenated text.
    rules, seen, n = {}, set(), 0
    for f, parsed in rules_by_file:
        for key in sorted(parsed, key=lambda k: int(k.removeprefix("policy"))):
            p = parsed[key]
            rid = p["annotations"]["id"]
            if rid in seen:
                problems.append(f"{f.name}: duplicate @id {rid!r}")
            seen.add(rid)
            rules[f"policy{n}"] = Rule(rid, p["effect"], p["annotations"].get("reason"),
                                       p["annotations"].get("summary"), f.name)
            n += 1
    if problems:
        raise Rejected(problems)

    policy_set = PolicySet(text, schema, rules, [f.name for f in files])
    failures = [msg for msg in check_matrix(policy_set)]
    if failures:
        raise Rejected(failures)
    return policy_set


def rule_problems(p: dict) -> list[str]:
    problems = []
    annotations = p.get("annotations", {})
    name = annotations.get("id", "a rule")
    if "id" not in annotations:
        problems.append("a rule has no @id")
    if p["effect"] == "forbid" and not annotations.get("reason"):
        problems.append(f"{name}: a forbid needs a @reason, which is what the approver is shown")
    if p["effect"] == "permit" and not annotations.get("summary"):
        problems.append(f"{name}: a permit needs a @summary saying what it allows")
    wide_open = all(p[k]["op"] == "All" for k in ("principal", "action", "resource")) and not p["conditions"]
    if wide_open:
        problems.append(f"{name}: applies to every principal, action and resource with no condition")
    if p["effect"] == "permit" and p["principal"]["op"] == "All":
        problems.append(f"{name}: a permit must name who it is for (is, == or in)")
    return problems


def payment(**overrides) -> dict:
    attrs = {"amountCents": 12000, "currency": "MYR", "vendor": "ACME", "vendorKnown": True, "priorPayments": 3,
             "paymentsLast24h": 0, "confidencePct": 95, "criticAgrees": True, "remitChanged": False,
             "submittedBy": "clerk"}
    return {"type": "Payment", "id": "p", "attrs": attrs | overrides}


AGENT = lambda name: {"type": "Agent", "id": name}  # noqa: E731
APPROVER = {"type": "User", "id": "amira", "roles": ["approver"]}
CONTROLLER = {"type": "User", "id": "rajesh", "roles": ["submitter", "approver", "controller"]}
CLERK = {"type": "User", "id": "clerk", "roles": ["submitter"]}
PERSON = CONTROLLER
VENDOR = {"type": "Vendor", "id": "v", "attrs": {"name": "ACME"}}

# The answers the policy set must give. A change that alters one fails validation,
# so loosening a rule needs this table changed too, in the same reviewed commit.
MATRIX = [
    ("agent approves a routine payment to a known vendor", AGENT("approval"), "ApprovePayment", payment(), "Allow"),
    ("agent may not approve above 5,000.00", AGENT("approval"), "ApprovePayment", payment(amountCents=500001), "Deny"),
    ("agent may approve exactly 5,000.00", AGENT("approval"), "ApprovePayment", payment(amountCents=500000), "Allow"),
    ("an approver may approve above 5,000.00", APPROVER, "ApprovePayment", payment(amountCents=600000), "Allow"),
    ("an approver may approve exactly 50,000.00", APPROVER, "ApprovePayment", payment(amountCents=5000000), "Allow"),
    ("an approver may not approve above 50,000.00", APPROVER, "ApprovePayment", payment(amountCents=5000001), "Deny"),
    ("a controller may approve above 50,000.00", CONTROLLER, "ApprovePayment", payment(amountCents=90000000), "Allow"),
    ("a clerk may not approve", CLERK, "ApprovePayment", payment(submittedBy="someone-else"), "Deny"),
    ("nobody approves their own invoice", CONTROLLER, "ApprovePayment", payment(submittedBy="rajesh"), "Deny"),
    ("an approver may not approve their own invoice", APPROVER, "ApprovePayment", payment(submittedBy="amira"),
     "Deny"),
    ("a person with no roles may not approve", {"type": "User", "id": "x"}, "ApprovePayment", payment(), "Deny"),
    ("agent may not approve a first-seen vendor", AGENT("approval"), "ApprovePayment",
     payment(vendorKnown=False, priorPayments=0), "Deny"),
    ("agent may not approve with 1 prior payment", AGENT("approval"), "ApprovePayment", payment(priorPayments=1),
     "Deny"),
    ("agent may not approve another currency", AGENT("approval"), "ApprovePayment", payment(currency="USD"), "Deny"),
    ("agent may not approve at 74% confidence", AGENT("approval"), "ApprovePayment", payment(confidencePct=74),
     "Deny"),
    ("agent may not approve over the critic", AGENT("approval"), "ApprovePayment", payment(criticAgrees=False),
     "Deny"),
    ("agent may not approve a changed pay-to account", AGENT("approval"), "ApprovePayment",
     payment(remitChanged=True), "Deny"),
    ("agent may approve a 3rd payment in 24 hours", AGENT("approval"), "ApprovePayment", payment(paymentsLast24h=2),
     "Allow"),
    ("agent may not approve a 4th payment in 24 hours", AGENT("approval"), "ApprovePayment",
     payment(paymentsLast24h=3), "Deny"),
    ("a person may approve a 4th payment in 24 hours", PERSON, "ApprovePayment", payment(paymentsLast24h=3), "Allow"),
    ("the risk agent may not approve", AGENT("risk-scoring"), "ApprovePayment", payment(), "Deny"),
    ("payment execution may not approve", AGENT("payment-execution"), "ApprovePayment", payment(), "Deny"),
    ("payment execution writes the ledger", AGENT("payment-execution"), "CreateLedgerEntry", payment(), "Allow"),
    ("payment execution dry-runs", AGENT("payment-execution"), "DryRunLedgerEntry", payment(), "Allow"),
    ("extraction may not write the ledger", AGENT("extraction"), "CreateLedgerEntry", payment(), "Deny"),
    ("risk scoring may not write the ledger", AGENT("risk-scoring"), "CreateLedgerEntry", payment(), "Deny"),
    ("approval may not write the ledger", AGENT("approval"), "CreateLedgerEntry", payment(), "Deny"),
    ("the critic may not write the ledger", AGENT("critic"), "CreateLedgerEntry", payment(), "Deny"),
    ("an unknown agent may not write the ledger", AGENT("payment-executor"), "CreateLedgerEntry", payment(), "Deny"),
    ("risk scoring reads vendor history", AGENT("risk-scoring"), "LookupVendor", VENDOR, "Allow"),
    ("extraction may not read vendor history", AGENT("extraction"), "LookupVendor", VENDOR, "Deny"),
    ("an approver may link a vendor alias", APPROVER, "ConfirmVendorAlias", VENDOR, "Allow"),
    ("a clerk may not link a vendor alias", CLERK, "ConfirmVendorAlias", VENDOR, "Deny"),
]


def check_matrix(policy_set: PolicySet) -> list[str]:
    failures = []
    for label, principal, action, resource, expected in MATRIX:
        got = policy_set.authorize(principal, action, resource)
        if got["errors"]:
            failures.append(f"{label}: evaluation error {got['errors']}")
        elif got["decision"] != expected:
            failures.append(f"expected {expected}, got {got['decision']}: {label}")
    return failures
