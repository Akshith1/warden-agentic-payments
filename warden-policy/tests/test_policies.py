"""The policy set and its validator, without Docker or a ledger. Scenario 07's
two bad changes are made on copies in a temporary folder."""
import shutil
from pathlib import Path

import pytest

from app.policyset import Rejected, load
from app.validate import main as validate

POLICIES = Path(__file__).resolve().parents[2] / "policies"


@pytest.fixture()
def copy(tmp_path):
    for f in POLICIES.iterdir():
        if f.is_file():
            shutil.copy(f, tmp_path)
    return tmp_path


def rejected_because(folder) -> str:
    with pytest.raises(Rejected) as e:
        load(folder)
    return " | ".join(e.value.problems)


def test_the_shipped_policies_are_accepted(capsys):
    assert validate(["validate", str(POLICIES)]) == 0
    assert "ACCEPTED" in capsys.readouterr().out


def test_a_wide_open_rule_is_rejected(copy, capsys):
    (copy / "99-allow-everything.cedar").write_text('@id("oops")\npermit (principal, action, resource);\n')
    assert validate(["validate", str(copy)]) == 1
    out = capsys.readouterr().out
    assert "REJECTED" in out and "every principal, action and resource" in out


def test_raising_the_threshold_is_caught_by_the_matrix(copy):
    # The subtle one: still valid Cedar, still schema-valid, but agents could now pay 5,000,000.00.
    f = copy / "01-routine-amount.cedar"
    f.write_text(f.read_text().replace("500000", "500000000"))
    assert "expected Deny, got Allow: agent may not approve above 5,000.00" in rejected_because(copy)


def test_deleting_the_rate_limit_is_caught(copy):
    (copy / "05-vendor-rate-limit.cedar").unlink()
    assert "4th payment in 24 hours" in rejected_because(copy)


def test_loosening_only_the_forbid_still_grants_nothing(copy):
    # Default deny: with no permit for other agents, a looser forbid changes no answer.
    f = copy / "06-ledger-write.cedar"
    f.write_text(f.read_text().replace('unless { principal.trustTier == "write-ledger" }',
                                       'unless { principal.trustTier != "none" }'))
    load(copy)


def test_granting_the_ledger_to_another_agent_is_caught(copy):
    f = copy / "06-ledger-write.cedar"
    text = f.read_text().replace('unless { principal.trustTier == "write-ledger" }',
                                 'unless { principal.trustTier != "none" }')
    f.write_text(text.replace('principal == Warden::Agent::"payment-execution"', "principal is Warden::Agent")
                 .replace('when { principal.trustTier == "write-ledger" }', 'when { principal.trustTier != "none" }'))
    assert "expected Deny, got Allow: approval may not write the ledger" in rejected_because(copy)


def test_a_forbid_without_a_reason_is_rejected(copy):
    f = copy / "03-ledger-currency.cedar"
    f.write_text("\n".join(line for line in f.read_text().splitlines() if not line.startswith("@reason")))
    assert "needs a @reason" in rejected_because(copy)


def test_a_permit_without_a_summary_is_rejected(copy):
    f = copy / "07-vendor-lookup.cedar"
    f.write_text("\n".join(line for line in f.read_text().splitlines() if not line.startswith("@summary")))
    assert "needs a @summary" in rejected_because(copy)


def test_an_attribute_not_in_the_schema_is_rejected(copy):
    f = copy / "03-ledger-currency.cedar"
    f.write_text(f.read_text().replace("resource.currency", "resource.currencyCode"))
    assert "schema" in rejected_because(copy)


def test_a_rule_that_does_not_parse_is_rejected(copy):
    (copy / "08-typo.cedar").write_text('@id("t")\npermit (principal is Warden::User action, resource);\n')
    assert "does not parse" in rejected_because(copy)
