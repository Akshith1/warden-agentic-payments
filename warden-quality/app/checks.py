"""What Warden expects of its data, as Great Expectations expectations.

Two kinds of check, both with a plain-English title and the reason it matters:

* **critical**: the ledger and the vendor history it feeds. These are the
  numbers money moves on. A failure stops every payment (the control plane
  trips the kill switch), because paying on a ledger that doesn't add up, or
  on a vendor history with no payment behind it, is worse than not paying.
* **warning**: reference and evaluation data (vendors loaded from USAspending,
  labelled receipts). A failure is shown and recorded; nothing stops.

The database already refuses some bad data on its own (a posting of zero, a
duplicate invoice key, an alias pointing at no vendor). These checks cover what
a single row can't show: totals across tables, history against payments,
whether loaded data is complete. Queries return ids only, never values, so a
result can be shown and logged without exposing personal data."""
from dataclasses import dataclass
from typing import Callable

import great_expectations as gx

CRITICAL, WARNING = "critical", "warning"
NET = "sum(CASE direction WHEN 'debit' THEN amount ELSE -amount END)"


@dataclass(frozen=True)
class Check:
    id: str
    suite: str          # ledger | vendors | inbox
    database: str       # ledger | backend
    table: str
    severity: str
    title: str
    why: str
    expectation: Callable[[], gx.expectations.Expectation]
    show_values: bool = False   # True only where the failing values are not personal (vendor names, labels)


def rows(query: str):
    # GX puts the table in for {batch}. Without a JOIN it adds its own alias, so a query of
    # that kind must not alias {batch} itself; with a JOIN the query names it.
    return lambda: gx.expectations.UnexpectedRowsExpectation(unexpected_rows_query=query)


CHECKS: list[Check] = [
    # --- The ledger: money ---------------------------------------------------------------------
    Check("ledger-balanced", "ledger", "ledger", "postings", CRITICAL,
          "Every transaction balances: its debits equal its credits",
          "Double entry is what makes the ledger checkable. One unbalanced transaction means money "
          "appeared or vanished.",
          rows(f"SELECT transaction_id AS id FROM {{batch}} GROUP BY transaction_id HAVING {NET} <> 0")),
    Check("ledger-two-sided", "ledger", "ledger", "transactions", CRITICAL,
          "Every transaction has at least two postings",
          "A transaction with one side, or none, can't balance and can't be traced.",
          rows("SELECT t.id FROM {batch} t LEFT JOIN postings p ON p.transaction_id = t.id "
               "GROUP BY t.id HAVING count(p.id) < 2")),
    Check("balances-match-postings", "ledger", "ledger", "accounts", CRITICAL,
          "Each account's balance equals the sum of its postings",
          "The balance is stored for speed and checked before every payment. If it drifts from the "
          "postings, the funds check is working from the wrong number.",
          rows(f"SELECT a.id FROM {{batch}} a LEFT JOIN (SELECT account_id, {NET} AS net FROM postings "
               "GROUP BY account_id) p ON p.account_id = a.id WHERE a.balance <> coalesce(p.net, 0)")),
    Check("no-overdraft", "ledger", "ledger", "accounts", CRITICAL,
          "No account that must stay positive is overdrawn",
          "Operating cash can't go below zero; the ledger refuses such a payment, so a negative "
          "balance means something wrote around it.",
          rows("SELECT id FROM {batch} WHERE NOT allow_negative AND balance < 0")),
    Check("history-backed-by-ledger", "ledger", "ledger", "historical_payments", CRITICAL,
          "Every Warden payment in vendor history matches a ledger payment of the same amount",
          "Vendor history earns trust: two settled payments let an invoice skip a person. A history "
          "row with no payment behind it would hand a vendor that trust for free.",
          rows("SELECT h.id FROM {batch} h LEFT JOIN transactions t ON t.idempotency_key = h.external_ref "
               "LEFT JOIN postings p ON p.transaction_id = t.id AND p.direction = 'debit' "
               "AND p.account_id = '5000-vendor-expense' WHERE h.source = 'warden' "
               "GROUP BY h.id, h.amount HAVING coalesce(sum(p.amount), 0) <> h.amount")),
    Check("payments-in-history", "ledger", "ledger", "transactions", WARNING,
          "Every payment reached the vendor's history",
          "History is written just after the ledger commit. A payment missing from it makes the "
          "vendor look newer than it is: safe, but it sends their next invoice to a person.",
          rows("SELECT id FROM {batch} WHERE idempotency_key <> 'opening-balance' AND NOT EXISTS "
               "(SELECT 1 FROM historical_payments h WHERE h.external_ref = idempotency_key)")),

    # --- Vendors: reference data ---------------------------------------------------------------
    Check("vendor-names-normalised", "vendors", "ledger", "vendors", WARNING,
          "Vendor keys are normalised (capitals, digits and single spaces)",
          "Lookups compare normalised names. A key in another form can never match, so that "
          "vendor's invoices always look first-seen.",
          lambda: gx.expectations.ExpectColumnValuesToMatchRegex(column="normalized_name",
                                                                 regex=r"^[A-Z0-9]+( [A-Z0-9]+)*$"),
          show_values=True),
    Check("alias-not-a-vendor", "vendors", "ledger", "vendor_aliases", WARNING,
          "No confirmed alias has the same name as a different vendor",
          "A name is looked up as a vendor first, so such an alias is never used and the link a "
          "person confirmed silently does nothing.",
          rows("SELECT a.normalized_alias AS id FROM {batch} a JOIN vendors v "
               "ON v.normalized_name = a.normalized_alias"),
          show_values=True),
    Check("history-amounts-positive", "vendors", "ledger", "historical_payments", WARNING,
          "Every historical payment is for more than zero",
          "Averages of past payments feed the risk score; zero or negative amounts drag it down.",
          lambda: gx.expectations.ExpectColumnValuesToBeBetween(column="amount", min_value=0.01)),
    Check("history-not-in-future", "vendors", "ledger", "historical_payments", WARNING,
          "No historical payment is dated in the future",
          "A future date counts as recent for the 24-hour limit for longer than it should.",
          rows("SELECT id FROM {batch} WHERE paid_on > current_date")),
    Check("reference-loaded", "vendors", "ledger", "vendors", WARNING,
          "Reference vendors are loaded (at least 10)",
          "Without the USAspending vendors every invoice is first-seen and goes to a person.",
          lambda: gx.expectations.ExpectTableRowCountToBeBetween(min_value=10)),

    # --- The inbox: evaluation data ------------------------------------------------------------
    Check("inbox-text-length", "inbox", "backend", "invoice_inbox", WARNING,
          "Every loaded receipt has between 20 and 20,000 characters of text",
          "An empty or runaway document scores as a failure in the evaluation gate and says "
          "nothing about the prompt.",
          lambda: gx.expectations.ExpectColumnValueLengthsToBeBetween(column="text", min_value=20,
                                                                      max_value=20000)),
    Check("inbox-split", "inbox", "backend", "invoice_inbox", WARNING,
          "Every loaded receipt is in the train or the test split",
          "The gate scores prompts on the test split only; a document in neither is never used.",
          lambda: gx.expectations.ExpectColumnValuesToBeInSet(column="split", value_set=["train", "test"]),
          show_values=True),
    Check("inbox-labels", "inbox", "backend", "invoice_inbox", WARNING,
          "Every loaded receipt has the labels the gate scores against",
          "A receipt without a labelled total can't be scored, so the gate would judge the prompt "
          "on fewer documents than it reports.",
          rows("SELECT id FROM {batch} WHERE (source = 'sroie' "
               "AND coalesce(ground_truth->'entities'->>'total', '') = '') "
               "OR (source = 'cord' AND ground_truth->'gt_parse' IS NULL)")),
    Check("eval-set-size", "inbox", "backend", "invoice_inbox", WARNING,
          "The evaluation gate has at least 20 labelled test documents",
          "Below that, one document moves the score by five points or more, and promotion is luck.",
          rows("SELECT 'test split' AS id WHERE (SELECT count(*) FROM {batch} WHERE split = 'test') < 20"),
          show_values=True),
]
