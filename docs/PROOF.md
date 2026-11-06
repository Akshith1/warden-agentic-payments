# What Warden has been shown to do

Each claim below was shown on a real machine, with real models and real data:
first on a Windows 11 PC with Docker Desktop, then on Azure. The evidence is
copied from the console, PowerShell and the Azure portal into the
[live-run notes](live-runs/), and each claim links to the note it comes from.
Unit tests and CI back every claim too. This page is only about what was seen
running.

Each live run also found something, and the notes say what it was and how it
was fixed. The last section lists what is still open.

## Routine invoices pay themselves; anything unusual goes to a person

- **The first fully automatic payment** (phase 2): a Brightline invoice for
  78.50 went through extraction, risk, approval and critic, then the dry run,
  the 20-second hold and the commit, with no person involved. One run
  earlier, the same vendor had been escalated by the approval model for
  reasons that were really business rules. Those rules now live in code, and
  the agents are told they're already enforced.
  [Phase 2](live-runs/2026-10-phase-2.md#first-fully-automatic-payment)
- **On Azure** (after phase 11): K-2026-201 (MYR 274.00) and a taxed invoice,
  K-2026-202 (MYR 328.60 with 6% SST), were each paid without a person. The
  approval agent was asked 3 times for each, and all 3 answers agreed.
  [Fixes](live-runs/2026-11-fixes.md)
- **A first-seen vendor, a vendor with too little history, a medium risk, or
  a critic's objection** all sent runs to a person. One example: the critic,
  on a different provider from the approval agent, objected that "Remit-to
  account is missing" on an invoice the approval agent was 92% sure of.
  [Phase 8](live-runs/2026-11-phase-8.md)

## Hidden instructions never reach an agent

A scanned receipt carried a note telling an AI to approve it. The OCR read it
inside a sandbox. Then the guardrail stopped it twice over: its fixed rules
found "NOTE TO AI" and "pre-approved", and the safety model found "an
instruction addressed to an AI to auto-approve the invoice". The run page
reads "Not read by the agents". The same thing happened on Docker Desktop
and on Azure. [Phase 5](live-runs/2026-11-phase-5.md),
[11b-3](live-runs/2026-11-phase-11b.md#scenario-08-on-azure-instructions-hidden-in-a-scanned-invoice)

## The OCR sandbox can't reach the network or write anything

The OCR service checks its own jail from inside the running container. On
Azure:

```
write a file in its job folder             REFUSED
read the OCR service's code                REFUSED
connect to 1.1.1.1:80                      REFUSED
```

Azure's kernel had an older Landlock that could not block network
connections, so Tesseract could reach the internet. The first self-test on
Azure showed `connect to 1.1.1.1:80  ALLOWED`. A seccomp filter now refuses
network sockets on any kernel.
[11b-3](live-runs/2026-11-phase-11b.md#phase-11b-3-scanned-invoices-on-azure)

## No model sees personal data

Names, emails, phone numbers and bank accounts are replaced with placeholders
before the guardrail or any agent sees the text. The real values come back
only for the person deciding and for the payment:

```
What the models saw:   Pay to Maybank account <BANK_ACCOUNT_1>
Pay to (console):      Maybank account 5140-9876-5432
```

When the masking service was down, the run failed closed: no model saw the
invoice, and **Retry** continued it once the service was fixed. Live runs
found four gaps the tests had missed, and each is now fixed:
- emails on `.example` domains weren't masked;
- item names were taken for people;
- an amount was taken for a phone number;
- an invoice number was taken for a phone number.

[Phase 7](live-runs/2026-11-phase-7.md),
[11a](live-runs/2026-11-phase-11a.md#finding-2-masking-garbled-the-invoices-item-table),
[11b-2](live-runs/2026-11-phase-11b.md#finding-masking-took-an-invoice-number-for-a-phone-number)

## A separate policy engine can overrule the agents

Four invoices from one vendor went in within milliseconds. Three were paid.
The fourth passed every agent (low risk, 97% sure, critic agreed) and was
refused by Cedar: "the vendor already had 3 payments approved in the last 24
hours, so a person must approve". When the person approved it, Cedar checked
that too, under a different rule.
[Phase 4](live-runs/2026-11-phase-4.md)

## Nobody approves their own invoice, and roles decide who may approve

- An approver who submitted an invoice was refused: "you submitted this
  invoice, so another person must approve it". A different approver then
  approved it.
- A submitter-only account sees the run but gets no decision buttons.
- The same held with Keycloak locally and with Entra ID on Azure.
  [Phase 6](live-runs/2026-11-phase-6.md),
  [11a](live-runs/2026-11-phase-11a.md#separation-of-duties)

## Only the backend's identity can write the ledger, and there is no write key

Locally, SPIRE gives each container a workload identity:
- the old write key, sent from the backend: `401 the ledger takes writes from workload identities only`;
- the policy service's genuine identity: `403 spiffe://warden.local/policy may not write the ledger`.

The key was then deleted from every file, and payments still went through. On
Azure, the backend's managed identity does the same job.
[Phase 6b](live-runs/2026-11-phase-6b.md), [11a](live-runs/2026-11-phase-11a.md#the-payment-path)

## Duplicates are stopped before money moves

- The same invoice submitted twice was approved by a person, then blocked by
  the ledger's dry run as a duplicate. [Phase 1](live-runs/2026-10-phase-1.md)
- The same invoice under a vendor's confirmed alias was stopped: "Already
  paid". [Phase 3.5](live-runs/2026-11-phase-3.md#phase-35-linking-the-names)
- The approval agent spotted five identical amounts to one vendor in 24 hours
  under different invoice numbers and escalated them. The ledger's duplicate
  check, which matches on invoice number, wouldn't have caught that.
  [Phase 9a](live-runs/2026-11-phase-9a.md#findings)

## A forged payment history stops all payments in about a second

Two history rows were written straight into the database to make a vendor
look trusted. Each row was valid on its own. The data-quality check that
compares vendor history with the ledger failed on both rows. One second
later, the watchdog had stopped all payments. The approver who ran the check
couldn't resume them; a controller had to.
[Phase 7b](live-runs/2026-11-phase-7b.md#a-forged-vendor-history-stops-payments)

## The kill switch holds, and the audit log records refusals too

With payments stopped, an approval was refused at once. After a controller
resumed payments, the four-eyes rule refused the same person, and that
refusal is in the audit log as well.
[Phase 9a](live-runs/2026-11-phase-9a.md)

## History can't be rewritten quietly

An insider with the database turned off the append-only trigger, edited
entry 3 and recomputed every hash after it. The hash chain verified again, as
expected. Against the write-once copy in Azure Blob storage, it failed: "entry
3 differs from the write-once copy… the database has been altered from entry
3 on, even if its chain verifies".
[11b](live-runs/2026-11-phase-11b.md#scenario-28-a-rewritten-history)

## Agent decisions are measured, and fixed by rule where the model varies

The consistency tool asks an agent the same question again with exactly the
same evidence.
- **Risk agent:** gave 0.46 to 0.85 on one invoice. At temperature 0, it gave
  0.78 to 0.85, high every time.
- **Approval agent:** said escalate 6 times and auto-approve 4 times on the
  same evidence. Its model ignores temperature. The fix is a rule: an
  auto-approval needs 3 of 3 asks to agree.
- **Rewritten approval criteria:** measured before and after on one run.
  - Before: 5 of 5 auto-approve, confidence 0.90 to 0.97.
  - After: 5 of 5 auto-approve, confidence 0.97 to 0.98.

[Phase 9b](live-runs/2026-11-phase-9b.md), [fixes](live-runs/2026-11-fixes.md)

## A worse prompt is blocked from production

The promotion gate scores candidate extraction prompts against labelled
receipts. One candidate passed on average (0.833 against 0.80), but it got
the vendor wrong on 10 of 20 receipts. The per-field floor blocked it:
`vendor 0.50 < floor 0.75`. A provider's rate limit is reported as
inconclusive, not counted as a wrong answer.
[Phase 2](live-runs/2026-10-phase-2.md#promotion-gate-third-run-the-per-field-floor-blocks-a-prompt)

## Each agent has its own key, routes and budget

On one invoice, five agents used five keys. That was about 7,000 tokens and
roughly one cent in total. The approval agent's call on the reasoning model
was the largest share. [Phase 8](live-runs/2026-11-phase-8.md#what-each-agent-used)

## Outside systems come in only through API Management

```
1. No subscription key                       401
2. A key but no token                        401
3. A token, straight to the backend          403
4. Key and token, through the gateway        202
6. A burst of 12 requests                    429 from call 8
```

[11b-2](live-runs/2026-11-phase-11b.md#playing-the-outside-system)

## It runs on Azure from one script

`.\infra\azure\deploy.ps1` deploys everything:
- Container Apps that scale to zero;
- Entra ID sign-in and managed identities;
- Key Vault;
- Postgres, the write-once audit copy and API Management;
- the data loader, data quality and Application Insights.

The first deploy took about 30 minutes; redeploys take about 5. The
Application Insights map draws the architecture from live calls.
[11a](live-runs/2026-11-phase-11a.md), [11c](live-runs/2026-11-phase-11c.md)

## Still open

These are known limits, not hidden ones:

- **Vendor names read from receipts vary.** A person links them as
  confirmed aliases, and nothing is matched automatically. Merging two
  vendors that already exist, and reversing a payment, aren't built.
  [Phase 3](live-runs/2026-11-phase-3.md), [5](live-runs/2026-11-phase-5.md)
- **The promoted extraction prompt sits exactly on the vendor floor** (0.75).
  It passes, but with no margin. [Phase 2](live-runs/2026-10-phase-2.md)
- **The risk agent can still draw "medium" on a clean invoice**: once in 5
  asks, at temperature 0. One such score sends a routine invoice to a person.
  [Fixes](live-runs/2026-11-fixes.md)
- **Not seen live, covered by tests:**
  - a kill-switch stop during the 20-second hold;
  - the gateway refusing an agent over budget;
  - the policy validator refusing a bad policy change;
  - whole capital names being masked.
- **The guardrail model isn't scored** by the gate; that needs a labelled set
  of injected and clean documents.
- **On Azure**, MLflow isn't deployed (the bundled prompt is used), the apps
  call each other over plain HTTP inside the environment, and the backend has
  a public address that admits only the console and API Management.
