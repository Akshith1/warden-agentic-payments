# Data map

What Warden holds, where it lives, and who or what can see it. Kept with the
code so a change to the data flow changes this file in the same pull request.

## Invoice content

| Data | Where it lives | Seen by models? | Who else sees it |
| --- | --- | --- | --- |
| Invoice text as submitted (pasted, or read from an image) | Run state in `warden_backend` (Postgres, LangGraph checkpoints) | **No.** Models see the masked text only | Signed-in people on the run page |
| Scanned invoice image | Sent to `warden-ocr`, held only in its job folder for the read, then deleted; dropped from the run once read | No | Nobody after OCR |
| Masked text: personal data replaced by placeholders such as `<PERSON_1>` | Run state | Yes: the guardrail and the Extraction agent | Signed-in people ("What the models saw") |
| Placeholder mapping (placeholder to real value) | Run state only; never returned by the API | No | Nobody through the API |
| Extracted fields (vendor, invoice number, date, amounts, pay-to account) | Run state; the ledger for paid invoices | Yes, re-masked: risk, approval and critic see placeholders for personal values | Signed-in people |

Personal data masked by `warden-pii` (Presidio with spaCy, plus receipt-specific rules): people's names, email addresses, phone numbers, card numbers, IBANs, bank account numbers after "account", Malaysian IC numbers, US SSNs, IP addresses. Business data is not masked: vendor names, business addresses, invoice numbers, dates, amounts.

## Money and vendors

| Data | Where it lives | Notes |
| --- | --- | --- |
| Accounts, transactions, postings | `warden_ledger` (Postgres) | Double entry; written only by the payment-execution role (Cedar + capability manifest) |
| Vendors, confirmed aliases, payment history | `warden_ledger` | Reference data from USAspending (public US federal awards); Warden's own settled payments |
| Labelled receipts for the evaluation gate | `invoice_inbox` in `warden_backend` | SROIE and CORD public datasets |

## Who reads the data to check it (phase 7b)

| Reader | Can read | Can't read | What it keeps |
| --- | --- | --- | --- |
| `warden-quality` (role `warden_quality`, `SELECT` only, read-only sessions) | Every table in `warden_ledger`; `invoice_inbox` in `warden_backend` | Run state (LangGraph checkpoints, which hold unmasked invoices); Keycloak; the audit log | The latest result, in memory: per check a count and up to five row ids. Values only where they aren't personal (vendor names, split labels). Results with failures, changes, and every run a person asked for go to the audit log. |

## People, decisions and the record

| Data | Where it lives | Notes |
| --- | --- | --- |
| Users, roles, passwords | Keycloak (its own volume) | Warden never sees a password |
| Who submitted and who decided each run | Run state and the audit log | Username from the verified token |
| Audit log | `warden-control` (SQLite in a volume), hash-chained | Steps, decisions, refusals, stops; the privacy step records only kinds and counts of personal data, never values |
| Model use per agent (tokens, cost) | `warden-control` | No content, only counts |

## The data catalogue (phase 7b, optional)

OpenMetadata, started with the `catalogue` profile, holds Warden's table and column names, types, descriptions, tags and lineage, never rows: ingestion is metadata only, with no profiler and no sample data. It reads the schema as `warden_quality`, so it sees the ledger and `invoice_inbox` and nothing else. Columns tagged as personal data there:

| Column | Tag | Why |
| --- | --- | --- |
| `historical_payments.remit_account` | PII.Sensitive | The account paid; a person's bank account when the payee is a sole trader |
| `invoice_inbox.text` | PII.Sensitive | Receipt text as read, with cashiers' names and phone numbers; masked before any model |
| `vendor_aliases.confirmed_by` | PII.NonSensitive | The username of the person who confirmed an alias |

The tags are set by `warden-catalogue/catalogue/classify.py`; a test keeps that file and this table in step.

## Telemetry (phase 9b)

| Data | Where it lives | Notes |
| --- | --- | --- |
| Traces: steps, model calls and service calls with timings | Tempo, in the `lgtm` container's volume | Attributes are ids, step names, vendor names, amounts, scores and decisions. Model calls carry the route, model and token counts, never the prompt or the answer |
| Metrics: counts and latencies | Prometheus, same volume | Labels are step, agent, route, outcome, decision and, for consistency checks, vendor |
| Step log: each trail entry as JSON | Loki, same volume | The audit log's facts, without an approver's note. Never invoice text |

Not instrumented: `warden-pii` and `warden-ocr`, which stay on their internal networks.

## Leaving the machine

Only the model gateway (LiteLLM) talks to model providers (OpenAI, Groq). What it sends is the masked text and re-masked extracted fields. The OCR and PII services have no route to the internet at all.
