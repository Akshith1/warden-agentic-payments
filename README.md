# Warden

**Governed agentic payments.** A team of AI agents reads invoices, scores them for fraud, decides whether they are safe to pay, and pays the safe ones through a real double-entry ledger, while governance layers decide what each agent is allowed to do and keep a record of every decision.

This repository is being built in phases (see [Roadmap](#roadmap)):

- **Phase 1, the agent core:** four LLM agents and a deterministic payment executor run as one LangGraph workflow, on real invoice data, with human-in-the-loop approval and a real ledger. Run live on real models; see [the phase 1 live run](docs/live-runs/2026-10-phase-1.md).
- **Phase 3, the console:** a web app at http://localhost:3100 to submit invoices, watch each agent's step as it happens, decide escalated invoices, retry failed runs, and see the ledger and the rules in force.
- **Phase 3.5, the vendor master:** when an approver recognises a "first-seen" vendor as one already paid under another name, they link the two in the console. The link is stored as a confirmed alias, so both names share one payment history and one duplicate-invoice check. Nothing is ever matched automatically. See [Vendor aliases](#vendor-aliases-phase-35) and [the phase 3 and 3.5 live run](docs/live-runs/2026-11-phase-3.md).
- **Phase 4, policies:** every approval and every ledger write is checked a second time by a separate Cedar policy service, with rules in readable files under `policies/`. The service adds a rule over time (no more than 3 agent-approved payments to one vendor in 24 hours) and refuses to start on a policy change that fails validation. Each role's capability manifest is a second, independent lock on the ledger. See [Policies](#policies-phase-4) and [the phase 4 live run](docs/live-runs/2026-11-phase-4.md).
- **Phase 5, runtime safety:** every document is screened by a guardrail before any agent reads it: fixed rules for known prompt-injection phrases plus the `gpt-oss-safeguard` safety model, and either one stops the run. Scanned invoices are read by a sandboxed OCR service with no network, a read-only filesystem and a kernel-enforced Landlock jail around Tesseract. See [Runtime safety](#runtime-safety-phase-5) and [the phase 5 live run](docs/live-runs/2026-11-phase-5.md).
- **Phase 8, per-agent model access:** each agent calls the model gateway with its own key. The gateway lets it use only its own routes, cuts off an agent paused by the kill switch, and refuses an agent that has used up its daily token budget. The console shows each agent's use against its budget. See [Model access per agent](#model-access-per-agent-phase-8) and [the phase 8 live run](docs/live-runs/2026-11-phase-8.md).
- **Phase 9a, kill switch and audit log:** anyone with the approver or controller role can stop all payments from the console. Only a controller can resume them. A payment in its settlement hold is stopped before money moves, and retried after the reset. The switch also trips by itself, on a canary vendor or an agent that keeps being refused. Every step of every run goes into a hash-chained audit log that shows exactly where it was edited. See [Kill switch and audit log](#kill-switch-and-audit-log-phase-9a) and [the phase 9a live run](docs/live-runs/2026-11-phase-9a.md).
- **Phase 6, identity:** people sign in to the console through Keycloak, and every approval is recorded under their own account. Approval is by role (approvers up to 50,000.00, controllers above), and nobody can approve an invoice they submitted. Cedar enforces both, and the backend verifies every token. See [Sign-in and roles](#sign-in-and-roles-phase-6) and [the phase 6 live run](docs/live-runs/2026-11-phase-6.md).
- **Phase 2, model governance:** the Extraction prompt is versioned in the MLflow Prompt Registry, and a DeepEval promotion gate scores candidate prompts against real labelled receipts. Only a prompt that clears the threshold is ever served. The live run's findings are now explicit rules, and failed runs can be retried. Run live, including the first fully automatic payment; see [the phase 2 live run](docs/live-runs/2026-10-phase-2.md).

## How a payment flows today

```
invoice text  or  scanned image -> Sandboxed OCR (warden-ocr: no network, read-only, Landlock jail)
   |
Guardrail ............... rules + safety model on the raw text; hidden instructions -> blocked
   |
Extraction agent ........ typed fields: vendor, number, date, total, line items, remit-to account
   |                      (blocked if the vendor or total is missing, or line items don't add up)
Risk Scoring agent ...... fraud score against real vendor history from the ledger
   |                      (first-seen vendor, fewer than MIN_PRIOR_PAYMENTS settled payments,
   |                       a currency other than LEDGER_CURRENCY, high risk, or a changed bank account -> human)
Approval agent .......... auto_approve / escalate / reject, with a confidence score
   |                      (over $5,000 or confidence < 0.75 -> human)
Critic agent ............ second opinion from a different provider; any concrete concern -> human
   |
Policy check ............ Cedar, in warden-policy: the same rules again, plus the 24-hour vendor limit
   |                      (agents refused -> human, with the policy's reason)
Human review ............ LangGraph interrupt(), durable in Postgres; resume with approve/reject
   |                      -> policy check again, for the person
Payment execution ....... no LLM: Cedar + capability manifest allow the write -> dry run
                          -> `settling` for a 20 s hold -> double-entry commit
                          -> vendor history updated only after settlement
```

| Agent | Model route | Typed output |
| --- | --- | --- |
| Extraction | `warden-routine` (Groq `gpt-oss-120b`) | `InvoiceExtraction` |
| Risk Scoring | `warden-routine` up to $5,000, `warden-reasoning` (OpenAI) above | `RiskAssessment` |
| Approval | `warden-reasoning` | `ApprovalDecision` |
| Critic | `warden-routine`, deliberately the other provider from Approval | `CriticReview` |
| Payment Execution | none, by design | `payment` in the run state |

The Extraction prompt is loaded at startup from the MLflow registry's `production` alias (the version the promotion gate chose), falling back to the bundled file if MLflow has nothing promoted or is down. `GET /health` and each run's audit trail show which one was used.

Agents never name a provider model. They call a route on the LiteLLM gateway, and every response is a Pydantic object validated by Instructor, which re-asks the model on a schema failure.

## Services

| Service | What it is | Local URL |
| --- | --- | --- |
| `warden-console` | Next.js console: runs, decisions, ledger, governance | http://localhost:3100 |
| `warden-backend` | FastAPI + LangGraph agent runtime | http://localhost:8100/docs |
| `warden-ledger` | Double-entry ledger, vendor master, payment history | http://localhost:8101/docs |
| `warden-policy` | Cedar policy decision point; validates `policies/` at startup | http://localhost:8102/docs |
| `warden-ocr` | Sandboxed OCR for scanned invoices; on an internal network only | (none: no published port) |
| `keycloak` | Sign-in for people: the `warden` realm, roles and users | http://localhost:8180 (admin at `/admin`) |
| `warden-control` | Kill switch and hash-chained audit log | http://localhost:8103/status |
| `litellm` | LLM gateway with the three routes; one key, route list and daily budget per agent | http://localhost:4000 |
| `mlflow` | Prompt Registry and gate results | http://localhost:5500 |
| `postgres` | One database and role per service | localhost:15432 |
| `data-loader` | One-shot job: SROIE + CORD invoices, USAspending vendors | (job) |
| `eval-gate` | One-shot job: scores candidate prompts, promotes the best passing one | (job) |

## Run it

You need Docker Desktop (about 5 GB of RAM is enough through phase 3), an OpenAI API key and a Groq API key.

On Windows PowerShell, use `.\infra\scripts\compose.ps1` wherever this README says `sh infra/scripts/compose.sh`; the arguments are the same.

```sh
cp .env.example .env            # then set OPENAI_API_KEY and GROQ_API_KEY
python infra/scripts/fill-secrets.py   # fresh random values for every local secret
docker network create warden-net
sh infra/scripts/compose.sh up -d --build
sh infra/scripts/compose.sh ps   # wait until postgres, warden-ledger and warden-backend show "healthy"
```

Load the real data (about two minutes; safe to run again, it never duplicates rows):

```sh
sh infra/scripts/compose.sh run --rm data-loader
```

Expect output like `USAspending (...): N new awards`, then `sroie test: 60 new documents` and so on.

Since phase 6 every backend call needs a sign-in (see [Sign-in and roles](#sign-in-and-roles-phase-6)); the console handles it for you. From a shell, get a token first:

```sh
TOKEN=$(curl -s -X POST localhost:8180/realms/warden/protocol/openid-connect/token \
  -d grant_type=password -d client_id=warden-cli -d username=clerk -d "password=<clerk's password>" \
  | python -c "import json,sys; print(json.load(sys.stdin)['access_token'])")
AUTH="Authorization: Bearer $TOKEN"
```

Submit a real receipt and follow it:

```sh
sh infra/scripts/compose.sh run --rm -e WARDEN_USERNAME=clerk data-loader python -m loader.submit --count 1
curl -s localhost:8100/runs -H "$AUTH" | python -m json.tool
curl -s localhost:8100/runs/<run_id> -H "$AUTH" | python -m json.tool
```

A vendor the ledger has never paid always goes to human review first. Approve it as someone with the approver role who didn't submit it (a token for `amira` or `rajesh`):

```sh
curl -s "localhost:8100/runs?status=pending_human" -H "$AUTH" | python -m json.tool
curl -s -X POST localhost:8100/runs/<run_id>/resume -H "$APPROVER_AUTH" \
  -H 'content-type: application/json' -d '{"decision": "approve"}'
```

After the 20-second settlement hold the run shows `settled`. Then check the money moved and the vendor now has history:

```sh
curl -s localhost:8101/accounts -H "X-API-Key: $(grep ^VENDOR_LOOKUP_KEY .env | cut -d= -f2)"
```

Submit another receipt from the same shop (SROIE repeats vendors) and it can now take the automatic path, because trust is earned from the ledger, not claimed by the invoice:

```sh
sh infra/scripts/compose.sh run --rm -e WARDEN_USERNAME=clerk data-loader python -m loader.submit --vendor "<shop name>" --count 2
```

You can also paste any invoice text: `POST /runs {"invoice_text": "..."}` at http://localhost:8100/docs (click **Authorize** and paste a token first).

If a run stops on a provider error (an exhausted API credit, a rate limit), it shows `failed` with the error. Fix the cause and continue it from the step that failed; finished steps are not repeated or re-billed:

```sh
curl -s -X POST localhost:8100/runs/<run_id>/retry -H "$AUTH"
```

## The console (phase 3)

Open http://localhost:3100 once the stack is up.

| Page | What you do there |
| --- | --- |
| Runs | Every invoice, newest first, refreshing by itself. A banner and a count show how many need your decision. |
| A run | The invoice as the agents read it, the trail of each agent's step with timings, and the outcome stamp. Approve or reject an escalated invoice, linking a first-seen vendor to an existing one if it is the same business; retry one that failed on a provider error. |
| Submit invoice | Paste invoice text, or submit one of the loaded SROIE receipts. |
| Ledger | Account balances and recent double-entry postings. |
| Governance | The Extraction prompt version in use and the rules enforced in code, read from the running backend. |

The browser only talks to the console's own server, which relays a fixed list of backend endpoints and read-only ledger endpoints; the ledger key stays on the server, and the console cannot write to the ledger. Pages refresh by polling every 1.5 to 5 seconds rather than over a WebSocket, which is enough for runs that change every few seconds. The approver's name is typed in until phase 6 adds Keycloak logins.

## Model access per agent (phase 8)

Until phase 8 every agent called the LiteLLM gateway with its master key, so any agent could call any model without limit. Now:

- **One key per agent.** A one-off step, `agent-keys-init`, generates a key for each agent into the `agent-keys` Docker volume, never into `.env`. Only LiteLLM, the backend and the eval-gate job mount it, read-only. The backend no longer has the master key, which stays with LiteLLM for administration.
- **The gateway checks every call** (`infra/litellm/warden_gateway.py`, LiteLLM's custom-auth hook) against `infra/litellm/agents.yaml`:

  | Agent | Routes | Daily tokens |
  | --- | --- | --- |
  | guardrail | warden-guardrail | 400,000 |
  | extraction | warden-routine | 400,000 |
  | risk-scoring | warden-routine, warden-reasoning | 400,000 |
  | approval | warden-reasoning | 300,000 |
  | critic | warden-routine | 300,000 |
  | eval-gate | warden-routine | 600,000 |

  An unknown key gets 401. A route outside the agent's list gets 403. So does an agent paused by the kill switch: pausing an agent in phase 9a now also cuts off its model access. An agent over today's budget gets 429 ("used its daily budget; it resets at midnight UTC"), and the run fails and can be retried after midnight UTC. Every refusal goes into the audit log. If the control plane can't be reached, no model call is made.
- **Spend is recorded per agent** after every call: tokens, and the cost in USD where LiteLLM knows the model's price. The Governance page shows each agent's use against its budget.

The budget is checked before each call, so the call that crosses it is the last one allowed; the next is refused. Budgets are in tokens rather than dollars because token counts don't depend on a price table that changes; the cost is recorded alongside.

Per-key budgets are also a built-in LiteLLM feature, but that needs its Prisma database client, whose engine download at build time adds a fragile step. The custom-auth hook gives the same enforcement point with the rules in this repository.

## Rotating secrets

`.env.example` once shipped with ready-made values for the local secrets, and they remain in this repository's history. If your `.env` was copied from it, replace them. Every published port now listens on `127.0.0.1` only, so nothing else on your network can reach these services.

- **`LITELLM_MASTER_KEY`, `LEDGER_WRITE_KEY`, `VENDOR_LOOKUP_KEY`, `KEYCLOAK_ADMIN_PASSWORD`**: delete the value after `=` in `.env`, run `python infra/scripts/fill-secrets.py`, then `up -d`. (Keycloak's admin password only applies to a new Keycloak volume; change it in the admin console otherwise.)
- **Postgres passwords**: they're set when the database volume is first created. For an existing install, change each role first, then `.env`, for example `compose.ps1 exec postgres psql -U postgres -c "ALTER ROLE warden_ledger PASSWORD '<new>'"`, then `up -d`.

## Kill switch and audit log (phase 9a)

**The kill switch** lives in `warden-control` and is shown at the top of the console's Governance page. While it is on, a red banner sits across every page, and payments are stopped at three points:

| Where | What happens |
| --- | --- |
| The policy engine | Every approval and ledger write is refused before any Cedar rule is read, so no rule can allow money to move. If the control plane can't be reached, the same applies. |
| A person's approval | Refused at once; the run keeps waiting. |
| The end of the settlement hold | Checked once more, right before the ledger commit. This is what the hold is for: a payment already approved and counting down is stopped before money moves. |

A run stopped this way fails with the reason, for example "Stopped by the kill switch", and can be retried once payments resume. The retry starts a fresh hold. Approvers and controllers may stop payments; only controllers may resume them.

The switch also trips by itself, recorded as the `watchdog`:
- **Canary vendor.** Any run naming `CANARY-DO-NOT-PAY` (set by `CANARY_VENDORS`) stops all payments. No genuine invoice uses that name, so seeing it means someone is probing the pipeline.
- **An agent refused again and again.** Five policy refusals of the same agent within ten minutes pause that agent alone. Its runs wait, and people can still decide escalated runs.

**The audit log** records every step of every run: who submitted it, each agent's step, each policy decision, each person's decision, refused approvals, and every stop and resume. Each entry is sealed with a SHA-256 hash of its content and the hash of the entry before it. Database triggers refuse changes. Anyone who removes the triggers and edits the file directly breaks the chain from that entry on, and the console's **Audit log** page says so and highlights the entries that can no longer be trusted. The run trail in each run's page is the working copy; the audit log is the record.

### Try it (scenarios 13, 14, 15 and 19 of the source project)

**13 and 19, stopping a payment in its hold.** Submit an invoice from a vendor with at least two settled payments, so the agents approve it themselves. One example is the OJC Marketing text from phase 3.5 with a new invoice number. When the run shows **In settlement hold**, open **Governance**, give a reason and click **Stop all payments**. Within the 20-second hold the run fails with "Stopped by the kill switch". Sign in as `rajesh`, click **Resume payments**, then **Retry from the failed step** on the run. It waits a fresh hold and pays.

**14, the canary.** Paste an invoice whose vendor is `CANARY-DO-NOT-PAY Sdn Bhd`. As soon as the Extraction agent has read it, payments stop everywhere, with "canary vendor CANARY-DO-NOT-PAY seen in run …" by `watchdog`. Resume as `rajesh`.

**15, tampering.** Try this one last: a broken chain stays broken, which is the point. With a few runs in the log, rewrite entry 3 the way someone with the database file would:
```powershell
.\infra\scripts\compose.ps1 exec warden-control python -m app.tamper --seq 3
```
It shows the triggers refusing a direct edit, then drops them and edits anyway. The **Audit log** page then reports "The chain is broken at entry 3". To start a fresh log afterwards, delete the `warden_control-data` volume: `.\infra\scripts\compose.ps1 rm -sf warden-control`, then `docker volume rm warden_control-data`, then `up -d`.

## Sign-in and roles (phase 6)

Until phase 5, the approver typed a name into a box. Now people sign in through Keycloak, and Warden takes their name and roles from a token Keycloak signed.

| Role | Can |
| --- | --- |
| `submitter` | Submit invoices, see runs, the ledger and the rules |
| `approver` | Decide escalated invoices up to 50,000.00; link vendor names |
| `controller` | Decide escalated invoices of any amount |

The realm (`infra/keycloak/realm-warden.json`) is imported on Keycloak's first start, with three people:

| User | Roles | First password |
| --- | --- | --- |
| `rajesh` | submitter, approver, controller | `change-me-rajesh` |
| `amira` | submitter, approver | `change-me-amira` |
| `clerk` | submitter | `change-me-clerk` |

Each first password works once. Keycloak makes you choose a new one (12 characters or more) at the first console sign-in, and refuses script sign-ins until you have.

How it holds together:

- **The console never sees a password.** "Sign in" sends you to Keycloak's own page. The console gets a code back and exchanges it for tokens with PKCE, then keeps them in httpOnly cookies that only its server reads. It sends the token with every backend call.
- **The backend checks every token itself** (`warden-backend/app/auth.py`): Keycloak's signature, issuer, audience `warden-backend` and expiry. Every endpoint except `/health` requires one. The approver's name comes only from the token, so nothing typed in a request can claim to be someone else.
- **Roles are checked twice.** The API turns away anyone without the right role. Cedar then decides each approval from the roles in the token (`policies/00-who-may-act.cedar`), including the tiers.
- **Four eyes** (`policies/08-four-eyes.cedar`): every run records who submitted it, and Cedar refuses an approval by that same person, whatever their role. The run stays waiting for someone else. The console shows the refusal as it happens, and the graph checks again before money moves.
- **Sign-out ends the Keycloak session too.** The next sign-in asks for the password again.

From PowerShell, `.\infra\scripts\token.ps1 <username>` asks for the password and returns headers for `Invoke-RestMethod`. The data loader's `submit` command signs in the same way (`-e WARDEN_USERNAME=clerk`).

### Try it

1. Open http://localhost:3100 and sign in as **clerk**. Choose a new password when asked.
2. Submit an invoice from a vendor the ledger has never paid. As clerk you can see it waiting, but there's no decision form: "Waiting for a person with the approver or controller role".
3. Sign out, sign in as **amira**, open the run and approve it. The trail shows "Approved by Amira Approver" and "Policy engine checked it: Allowed for amira (approvers-may-approve)".
4. Still as amira, submit another invoice and try to approve it yourself. Cedar refuses: "you submitted this invoice, so another person must approve it".

## Runtime safety (phase 5)

An invoice is untrusted input written by whoever sent it, and the Extraction agent is a language model that follows instructions. Phase 5 stops a document from instructing the agents, and keeps a malicious image file from harming the system that reads it.

**The guardrail** runs first on every document, pasted or scanned. Only the Extraction agent ever reads raw text; every later agent sees its typed fields, so this one checkpoint covers them all. It has two independent layers:

| Layer | What it is | Why both |
| --- | --- | --- |
| Rules | Fixed patterns for known injection shapes: "ignore previous instructions", "system:", "note to the AI", "pre-approved", "do not send for review" (`warden-backend/app/agents/guardrail.py`) | Predictable and free; they can't be argued out of a match |
| Safety model | `gpt-oss-safeguard-20b` on the `warden-guardrail` route, given a written policy (`prompts/guardrail.txt`) with four categories: instructions to the AI, self-approval, payment redirection, other | Catches rewordings no rule lists |

If either layer flags the document, the run is **blocked**, with the words that triggered it in the reason and the trail. It isn't sent to a person: approving it would still hand the text to the Extraction agent. If the guardrail model can't be reached, the run fails and can be retried; it never passes unchecked.

**The OCR sandbox.** A scanned invoice (PNG, JPEG or TIFF, up to 5 MB) goes to `warden-ocr`, never to the backend's own disk. Image decoders are a classic attack surface, so the service is locked down in layers:

| Layer | Set by |
| --- | --- |
| No route to the internet: its only network is `warden-ocr`, an internal Docker network shared with the backend alone | `docker-compose.app.yml` |
| Read-only root filesystem, non-root user (uid 10001), all Linux capabilities dropped, no privilege escalation, 64 processes, 1.5 GB memory | `docker-compose.app.yml` |
| Each image is read by a separate Tesseract process under a **Landlock** ruleset: it can read only the system libraries and its own job folder, can write nowhere, and can't open a network connection. The kernel enforces this, so it holds even if Tesseract is compromised. CPU time, memory and file size are capped too. | `warden-ocr/app/jail.py`, `landlock.py` |
| The job folder is deleted after each image, and the backend drops the image from the run once it has the text | `warden-ocr/app/main.py`, `agents/ocr.py` |

If the kernel has no Landlock, the service refuses to read images rather than reading them unsandboxed (`/health` reports `"unsandboxed"`).

### Try it (scenarios 08 and 11 of the source project)

**08, hidden instructions.** On **Submit invoice**, upload `docs/samples/receipt-injected.png`, an invented receipt with a "note to AI assistant" in its footer. The trail shows the sandboxed OCR, then the guardrail stopping it with the words it found. No agent runs. For comparison, `docs/samples/receipt-clean.png` is the same receipt without the note. It passes the guardrail and goes to you as a first-seen vendor. You can also paste text with an instruction in it.

**11, what the sandbox refuses:**
```powershell
.\infra\scripts\compose.ps1 exec warden-ocr python -m app.selftest
```
This prints the kernel's Landlock version, the user (not root) and the read-only root filesystem. It then lists what a process inside the jail tried and was refused: writing anywhere, reading the service's own code, and connecting to the internet or to the ledger.

## Policies (phase 4)

The rules that decide when agents may pay on their own were enforced in Python. Phase 4 writes them again as [Cedar](https://www.cedarpolicy.com) policies in `policies/`, served by a separate service, `warden-policy`. A payment needs both to agree: the code rules, then the policy check. Either one saying no sends the invoice to a person, with the reason in the trail.

| File | Rule |
| --- | --- |
| `00-who-may-act.cedar` | Only the approval agent and people may approve; only people may link vendor names. Everything else is denied by default. |
| `01-routine-amount.cedar` | Agents may not approve above 5,000.00. |
| `02-vendor-history.cedar` | Agents may not approve a vendor with fewer than 2 settled payments. |
| `03-ledger-currency.cedar` | Agents may not approve a currency other than MYR. |
| `04-decision-quality.cedar` | Agents may not approve below 75% confidence, over the critic's objection, or to a changed pay-to account. |
| `05-vendor-rate-limit.cedar` | Agents may not approve a 4th payment to one vendor within 24 hours. |
| `06-ledger-write.cedar` | Only the payment-execution agent may write to the ledger. |
| `07-vendor-lookup.cedar` | Only the risk agent and people may read vendor history. |

How it is kept honest:

- **Facts come from the ledger, not the caller.** The backend sends what the run knows (amount, currency, confidence, the critic's answer). The policy service looks up the vendor's history, payment count and recent payments itself. Agent trust tiers come from the service's own registry (`warden-policy/app/agents.json`), so a caller claiming `write-ledger` changes nothing.
- **A rule over time.** Cedar judges one request and remembers nothing, so the service supplies the 24-hour count: the larger of the vendor's settled payments in the last 24 hours and the agent approvals it allowed in that window. The second number catches a burst submitted together, before any of it settles.
- **Two locks on the ledger.** Each role has a capability manifest in `warden-backend/app/capabilities/`, and nodes reach the ledger only through a guard that checks it in plain code. Cedar checks the same writes independently, so neither a policy mistake nor a code mistake alone can give an agent the ledger.
- **A bad change can't be served.** At startup the service validates the whole set: every file parses, the set matches the schema, no rule is wide open, every rule has an `@id`, every permit a `@summary` and every forbid a `@reason`, and 25 authorization cases give the expected answers. If any check fails, the service doesn't start. With no policy service, runs stop as `failed` and can be retried once it's back. They don't pay without it.
- **Visible.** The console's Governance page lists every rule and every manifest, and each run's trail shows the policy check with the rule that decided it.

### Try it (scenarios 06, 07, 09, 10 and 20 of the source project)

Run these in PowerShell from the repository folder, with the stack up.

**06, a policy set is accepted:**
```powershell
.\infra\scripts\compose.ps1 exec warden-policy python -m app.validate /policies
```

**07, a bad change is rejected.** These commands work on copies in `.scratch\` (gitignored), never on `policies\`. First, a rule that allows everything:
```powershell
New-Item -ItemType Directory -Force .scratch\bad-a | Out-Null
Copy-Item policies\* .scratch\bad-a\
Set-Content .scratch\bad-a\99-allow-everything.cedar "@id(`"oops`")`npermit (principal, action, resource);"
.\infra\scripts\compose.ps1 exec warden-policy python -m app.validate /scratch/bad-a
```
Then a subtle one: valid Cedar, but agents could now pay up to 5,000,000.00:
```powershell
New-Item -ItemType Directory -Force .scratch\bad-b | Out-Null
Copy-Item policies\* .scratch\bad-b\
(Get-Content .scratch\bad-b\01-routine-amount.cedar) -replace '500000', '500000000' | Set-Content .scratch\bad-b\01-routine-amount.cedar
.\infra\scripts\compose.ps1 exec warden-policy python -m app.validate /scratch/bad-b
```
Both print `REJECTED` with the reason.

**09 and 10, agents can't reach past their role.** Ask Cedar whether the extraction agent may write to the ledger:
```powershell
$body = '{"principal":{"type":"Agent","id":"extraction"},"action":"CreateLedgerEntry","payment":{"amount":"10.00","currency":"MYR","vendor":"OJC Marketing Sdn Bhd"}}'
Invoke-RestMethod -Method Post -Uri http://localhost:8102/authorize -ContentType "application/json" -Body $body
```
Then ask the second lock, the capability manifests:
```powershell
.\infra\scripts\compose.ps1 exec warden-backend python -m app.capability
```
Cedar returns `Deny`. The manifests deny every role except payment execution.

**20, the 24-hour limit.** Submit four invoices from a vendor with history. The first three are normally paid by the agents (unless one of them escalates for its own reasons); the fourth goes to a person with "the vendor already had 3 payments approved in the last 24 hours":
```powershell
$h = .\infra\scripts\token.ps1 clerk     # asks for clerk's password
foreach ($n in 461..464) {
  $text = "OJC Marketing Sdn Bhd`nTax Invoice No: OJC-2026-0$n`nDate: 02/11/2026`nPrinting paper A4 x 5   RM 42.50`nInk cartridge black x 1   RM 22.00`nTotal: RM 64.50"
  Invoke-RestMethod -Method Post -Uri http://localhost:8100/runs -Headers $h -ContentType "application/json" -Body (@{invoice_text = $text} | ConvertTo-Json)
}
```

## Vendor aliases (phase 3.5)

The same shop can be read under two names. In the phase 3 live run, a receipt from a vendor already paid as `TAN CHAY YEE *** COPY *** OJC MARKETING SDN BHD` came back as `TAN CHAY YEE OJC MARKETING SDN BHD`, so the ledger treated it as a vendor it had never paid.

When an invoice is escalated as a first-seen vendor, the decision form asks **"Is this a vendor you already pay?"** and lists ledger vendors with similar names, each with its payment count and last pay-to account. It flags a vendor whose last account differs from the invoice. If the approver picks one and approves:

1. The backend records the invoice's vendor name as an alias of the chosen vendor (`POST /vendors/aliases` on the ledger, with the write key), with the approver's name and the run id.
2. The review step reads the vendor back from the ledger, and the trail shows "Confirmed as ..., which has N earlier payments".
3. The payment's duplicate-invoice key uses the vendor's own ledger name, so the same invoice read later under either name is refused as a duplicate.
4. Future invoices under the alias are known vendors with the full history, and their payments count toward that history.

Rules that keep this from becoming a fraud path:

- **No automatic matching.** `GET /vendors/search` only lists candidates and links nothing. Look-alike vendor names are a classic way to redirect payments, so only a person's confirmation creates a link.
- **Only on approval, only for first-seen vendors.** Linking with a rejection is refused (422), as is relinking a name the ledger already knows (409).
- **An alias belongs to one vendor.** Pointing an existing alias, or another vendor's own name, at a different vendor is refused (409). Repeating the same confirmation is harmless.
- **The console still can't write to the ledger.** The link travels through the backend's resume endpoint; the console's ledger proxy stays read-only.

The approver's name is typed in until phase 6 takes it from the login.

## Prompt promotion gate (phase 2)

The gate scores each candidate Extraction prompt on held-out SROIE test receipts, through the same code path production uses, against SROIE's ground-truth labels. The metric is deterministic, not an LLM judge: vendor name (token overlap of at least 0.8, so a cashier's name or a "COPY" stamp in the vendor counts as a miss), date (same calendar date in any common format) and total (within 0.01). A candidate passes only if its overall accuracy reaches `PROMOTION_THRESHOLD` (default 0.80) **and** every field reaches `FIELD_FLOOR` (default 0.75); the floor was added after the first live gate run showed an average can hide a broken vendor field (see [the phase 2 live run](docs/live-runs/2026-10-phase-2.md)). Every candidate is registered in MLflow with its score and, if blocked, why; only the best passing one gets the `production` alias.

Two candidates ship in `warden-backend/app/prompts/`: `extraction.txt` (structured, with explicit rules) and `candidates/extraction-casual.txt` (the kind of one-line prompt a first draft often is).

```sh
sh infra/scripts/compose.sh run --rm eval-gate        # a few minutes: 40 receipts x 2 prompts, paced
sh infra/scripts/compose.sh restart warden-backend    # load the promoted prompt
curl -s localhost:8100/health                         # extraction_prompt: {"source": "mlflow", "version": "..."}
```

It prints a table of accuracy per candidate and field and a verdict (PROMOTED, passed or BLOCKED); the per-document results are in MLflow at http://localhost:5500 under the `warden-prompt-gate` experiment and the `warden-extraction` prompt. It calls Groq only (`warden-routine`), 80 calls at the default sample size, paced for the free tier.

**Groq free-tier budget.** `gpt-oss-120b` on the free tier allows 200,000 tokens per day (rolling 24 hours), and one gate run at 40 receipts uses most of that; the agents' own calls share the same budget. On the free tier, run the gate at most once a day, or set `EVAL_SAMPLE_SIZE=20`. If the provider refuses a call (429, 5xx, timeout), the gate stops as **INCONCLUSIVE** (exit code 3), registers nothing and leaves `production` unchanged: a refused call says nothing about the prompt, so it is never scored as a wrong answer.

## Upgrading from phase 1

Phase 2 adds an `mlflow` database and four settings. Your existing data is kept.

1. `git pull`
2. Add these lines to your `.env` (any random value for the password: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`):
   ```
   PGPASS_MLFLOW=<random value>
   LEDGER_CURRENCY=MYR
   MIN_PRIOR_PAYMENTS=2
   EVAL_SAMPLE_SIZE=40
   PROMOTION_THRESHOLD=0.80
   FIELD_FLOOR=0.75
   ```
3. Create the new database in the existing Postgres (the init script is idempotent and changes nothing else):
   ```sh
   sh infra/scripts/compose.sh up -d postgres
   sh infra/scripts/compose.sh exec postgres sh /docker-entrypoint-initdb.d/01-create-service-databases.sh
   ```
4. `sh infra/scripts/compose.sh up -d --build`, then run the gate as above.

Runs that failed on a provider error in phase 1 (shown as `blocked` with `error: ...`) are relabelled `failed` on startup and can be retried.

## Tests

The tests run against a real Postgres and a real ledger process. Only the LLM is replaced by a test double, so routing and money movement are checked deterministically; model quality is the job of the eval gate in phase 2.

```sh
# Postgres with the service databases (for example the compose postgres on port 15432)
export POSTGRES_HOST=localhost POSTGRES_PORT=15432
export $(grep -E '^PGPASS_WARDEN_(LEDGER|BACKEND)=' .env | xargs)

cd warden-ledger && pip install -r requirements.txt -r requirements-dev.txt && pytest -q && cd ..
cd warden-policy && pip install -r requirements.txt -r requirements-dev.txt && pytest -q && cd ..
cd warden-control && pip install -r requirements.txt -r requirements-dev.txt && pytest -q && cd ..
cd warden-ocr && pip install -r requirements.txt -r requirements-dev.txt && pytest -q && cd ..   # needs tesseract; Landlock tests skip without it
cd warden-backend && pip install -r requirements.txt -r requirements-dev.txt && pytest -q   # starts the ledger, policy and control services itself
```

They cover the happy path, first-seen vendors, human approve and reject, the $5,000 threshold and its route switch, low confidence, critic disagreement, a changed remit-to account, duplicate invoices, insufficient funds, missing totals, line items that don't add up, and the API's double-resume guard. Phase 2 adds the history and currency rules, the `settling` status, retrying a failed run without repeating finished steps, the gate's scoring rules, and the gate end to end against a real MLflow server (set `MLFLOW_TRACKING_URI` to include those; they are skipped otherwise). Phase 3.5 adds vendor aliases: a linked name shares the vendor's history, the same invoice under either name is a duplicate, search links nothing, and conflicting or out-of-place links are refused. Phase 8 adds spend per agent against its budget and each agent calling with its own key; the gateway hooks were tested against a running LiteLLM 1.106.5 with mock models (route refused, budget refused, paused agent refused, spend recorded). Phase 9a adds the chain catching an edited and a deleted entry, the tamper script, both automatic triggers, roles on stop and resume, the kill switch overriding every Cedar rule, a payment stopped during its hold and paid on retry, a paused agent while people can still decide, and every step of a twice-stopped run recorded once and in order. Phase 6 adds token checks with real signed tokens (good, wrong key, wrong issuer, wrong audience, expired), roles on every endpoint, approval tiers, and four eyes in both the API and the graph. Phase 5 adds the guardrail's rules against injected and ordinary receipt text, blocked runs that no agent ever reads, the safety model catching a rewording, failing closed when the guardrail is down, scanned invoices through OCR, and (in `warden-ocr`) real Tesseract under a real Landlock jail. Phase 4 adds the policy set and its validator (including both bad changes from scenario 07), the burst-proof 24-hour limit, Cedar catching a rule the code got wrong, a person refused by policy, no payment without the policy service, and the capability manifests.

## Known simplifications

These are deliberate and each one is removed by a named later phase.

- **Secrets live in `.env`.** Phase 6 moves the ledger write key into Infisical, fetched fresh before every payment.
- **The approval rules are written twice**, in code (`config.py`) and in Cedar (`policies/`), on purpose: two independent checks. Changing a limit means changing both; the stricter one wins until they agree.
- **The 24-hour limit's memory of in-flight approvals is in the policy service's memory.** After a restart only the ledger's settled payments count until new approvals come in. Phase 9's audit log is the durable record.
- **Keycloak runs in development mode** with its own file database in a Docker volume, over plain HTTP on localhost. The Azure profile (phase 11) puts sign-in behind TLS with Entra ID.
- **The ledger's write key and the gateway key are still in `.env`.** Phase 6b moves them into a secrets vault (Infisical), fetched fresh before each use, and gives each service its own workload identity (SPIRE).
- **Only the Extraction prompt is registry-managed.** It is the one the gate can score against labels. The Risk, Approval and Critic prompts are versioned in git; scoring them needs labelled fraud decisions, which no public dataset here provides.
- **A promoted prompt takes effect on restart**, not live, matching how the backend loads it once at startup.
- **The audit log is a SQLite file in a Docker volume.** The hash chain makes edits evident but can't stop someone deleting the whole file. The Azure profile (phase 11) writes it to Blob storage with an immutability policy, which can't be deleted for the retention period.
- **Any service on the Docker network can append to the audit log.** It can't change past entries. Phase 6b's workload identities limit who may write.
- **The guardrail blocks; it doesn't ask.** A legitimate invoice that happens to say "pre-approved" is blocked, and the sender has to resubmit without it. That is the safer failure for a payments system. The rules' wording is tested against ordinary receipt text in `tests/test_guardrail.py`.
- **The guardrail model isn't scored by the evaluation gate yet.** The gate scores extraction against labelled receipts. Scoring the guardrail needs a labelled set of injected and clean documents, which can come from the live runs.
- **Single-currency ledger, no conversion.** The ledger is denominated in `LEDGER_CURRENCY` (MYR by default, matching the SROIE receipts). A document showing another currency is escalated, never converted. The USAspending vendor history is in US dollars and is used only as a reference vendor set; it shares no vendors with the receipts.

## Roadmap

| Phase | Adds | Proven by scenarios |
| --- | --- | --- |
| 1 | Agent core (done) | 01 |
| 2 | MLflow prompt registry, DeepEval gate, vendor trust loop (done) | 04, 05, 21 |
| 3 | Next.js console: submit, live run view, approval queue (done) | 01 from the browser |
| 3.5 | Vendor master: human-confirmed aliases (done) | phase 3 live-run finding |
| 4 | Cedar policies, 24-hour vendor limit, capability manifests (done) | 06, 07, 09, 10, 20 |
| 5 | Guardrail (rules + safety model), sandboxed OCR with Landlock (done); confidence gate, dry run and hold were built in phases 1-2 | 08, 11, 16, 17, 18 |
| 6 | Keycloak sign-in, approval roles and tiers, four eyes (done) | identity |
| 6b | Infisical secrets vault, SPIRE workload identity | 02 |
| 7 | OpenMetadata, Presidio, Great Expectations | 03, 22 |
| 8 | Per-agent gateway keys, route allowlists, daily token budgets, spend per agent (done) | 12 |
| 9a | Kill switch (manual, canary, repeated refusals), hash-chained audit log (done) | 13, 14, 15, 19 |
| 9b | OpenTelemetry, Langfuse, Grafana | 24-27 |
| 10 | GitHub Actions: policy validation, data-quality self-test, eval gate | CI |
| 11 | Azure profile (Container Apps, Entra ID, Key Vault, APIM, Blob WORM) | 28-31 |

## Data sources

- [SROIE](https://huggingface.co/datasets/jsdnrs/ICDAR2019-SROIE) and [CORD v2](https://huggingface.co/datasets/naver-clova-ix/cord-v2): real receipts with ground-truth labels, via Hugging Face `datasets`.
- [USAspending.gov](https://api.usaspending.gov/) `spending_by_award`: real contract recipients, amounts and dates for the vendor master.

## Acknowledgements

The governance scope of this project was inspired by the "Financial Agent with AI Governance" course project ([data-guru0/Financial-Agent-with-AI-Governance](https://github.com/data-guru0/Financial-Agent-with-AI-Governance)). Warden is an independent implementation with its own code, design and Azure profile.
