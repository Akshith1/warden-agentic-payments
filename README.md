# Warden

**Governed agentic payments.** A team of AI agents reads invoices, scores them for fraud, decides whether they are safe to pay, and pays the safe ones through a real double-entry ledger, while governance layers decide what each agent is allowed to do and keep a record of every decision.

This repository is being built in phases (see [Roadmap](#roadmap)):

- **Phase 1, the agent core:** four LLM agents and a deterministic payment executor run as one LangGraph workflow, on real invoice data, with human-in-the-loop approval and a real ledger. Run live on real models; see [the phase 1 live run](docs/live-runs/2026-10-phase-1.md).
- **Phase 2, model governance:** the Extraction prompt is versioned in the MLflow Prompt Registry, and a DeepEval promotion gate scores candidate prompts against real labelled receipts. Only a prompt that clears the threshold is ever served. The live run's findings are now explicit rules, and failed runs can be retried. Run live, including the first fully automatic payment; see [the phase 2 live run](docs/live-runs/2026-10-phase-2.md).

## How a payment flows today

```
invoice text
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
Human review ............ LangGraph interrupt(), durable in Postgres; resume with approve/reject
   |
Payment execution ....... no LLM: dry run -> `settling` for a 20 s hold -> double-entry commit
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
| `warden-backend` | FastAPI + LangGraph agent runtime | http://localhost:8100/docs |
| `warden-ledger` | Double-entry ledger, vendor master, payment history | http://localhost:8101/docs |
| `litellm` | LLM gateway with the three routes | http://localhost:4000 |
| `mlflow` | Prompt Registry and gate results | http://localhost:5500 |
| `postgres` | One database and role per service | localhost:15432 |
| `data-loader` | One-shot job: SROIE + CORD invoices, USAspending vendors | (job) |
| `eval-gate` | One-shot job: scores candidate prompts, promotes the best passing one | (job) |

## Run it

You need Docker Desktop (about 5 GB of RAM is enough through phase 2), an OpenAI API key and a Groq API key.

On Windows PowerShell, use `.\infra\scripts\compose.ps1` wherever this README says `sh infra/scripts/compose.sh`; the arguments are the same.

```sh
cp .env.example .env            # then set OPENAI_API_KEY and GROQ_API_KEY
docker network create warden-net
sh infra/scripts/compose.sh up -d --build
sh infra/scripts/compose.sh ps   # wait until postgres, warden-ledger and warden-backend show "healthy"
```

Load the real data (about two minutes; safe to run again, it never duplicates rows):

```sh
sh infra/scripts/compose.sh run --rm data-loader
```

Expect output like `USAspending (...): N new awards`, then `sroie test: 60 new documents` and so on.

Submit a real receipt and follow it:

```sh
sh infra/scripts/compose.sh run --rm data-loader python -m loader.submit --count 1
curl -s localhost:8100/runs | python -m json.tool
curl -s localhost:8100/runs/<run_id> | python -m json.tool
```

A vendor the ledger has never paid always goes to human review first. Approve it:

```sh
curl -s "localhost:8100/runs?status=pending_human" | python -m json.tool
curl -s -X POST localhost:8100/runs/<run_id>/resume \
  -H 'content-type: application/json' -d '{"decision": "approve", "approver": "controller-1"}'
```

After the 20-second settlement hold the run shows `settled`. Then check the money moved and the vendor now has history:

```sh
curl -s localhost:8101/accounts -H "X-API-Key: $(grep ^VENDOR_LOOKUP_KEY .env | cut -d= -f2)"
```

Submit another receipt from the same shop (SROIE repeats vendors) and it can now take the automatic path, because trust is earned from the ledger, not claimed by the invoice:

```sh
sh infra/scripts/compose.sh run --rm data-loader python -m loader.submit --vendor "<shop name>" --count 2
```

You can also paste any invoice text: `POST /runs {"invoice_text": "..."}` at http://localhost:8100/docs.

If a run stops on a provider error (an exhausted API credit, a rate limit), it shows `failed` with the error. Fix the cause and continue it from the step that failed; finished steps are not repeated or re-billed:

```sh
curl -s -X POST localhost:8100/runs/<run_id>/retry
```

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
cd warden-backend && pip install -r requirements.txt -r requirements-dev.txt && pytest -q
```

They cover the happy path, first-seen vendors, human approve and reject, the $5,000 threshold and its route switch, low confidence, critic disagreement, a changed remit-to account, duplicate invoices, insufficient funds, missing totals, line items that don't add up, and the API's double-resume guard. Phase 2 adds the history and currency rules, the `settling` status, retrying a failed run without repeating finished steps, the gate's scoring rules, and the gate end to end against a real MLflow server (set `MLFLOW_TRACKING_URI` to include those; they are skipped otherwise).

## Known simplifications

These are deliberate and each one is removed by a named later phase.

- **Secrets live in `.env`.** Phase 6 moves the ledger write key into Infisical, fetched fresh before every payment.
- **One gateway key for all agents.** Phase 8 gives each agent its own key, route allowlist and daily budget.
- **Approval rules are enforced in code only.** Phase 4 adds Cedar as an independent second check, plus capability manifests.
- **Only the Extraction prompt is registry-managed.** It is the one the gate can score against labels. The Risk, Approval and Critic prompts are versioned in git; scoring them needs labelled fraud decisions, which no public dataset here provides.
- **A promoted prompt takes effect on restart**, not live, matching how the backend loads it once at startup.
- **No guardrail model, OCR sandbox or kill switch yet.** Phases 5 and 9.
- **Single-currency ledger, no conversion.** The ledger is denominated in `LEDGER_CURRENCY` (MYR by default, matching the SROIE receipts). A document showing another currency is escalated, never converted. The USAspending vendor history is in US dollars and is used only as a reference vendor set; it shares no vendors with the receipts.

## Roadmap

| Phase | Adds | Proven by scenarios |
| --- | --- | --- |
| 1 | Agent core (done) | 01 |
| 2 | MLflow prompt registry, DeepEval gate, vendor trust loop (done) | 04, 05, 21 |
| 3 | Next.js console: submit, live run view, approval queue | 01 from the browser |
| 4 | Cedar policies, temporal rate limit, capability manifests | 06, 07, 09, 10, 23 |
| 5 | Guardrail model, sandboxed OCR (Landlock), confidence gate, dry run, hold | 08, 11, 16, 17, 18 |
| 6 | Keycloak, SPIRE, Infisical | 02, 20 |
| 7 | OpenMetadata, Presidio, Great Expectations | 03, 22 |
| 8 | Per-agent LiteLLM keys and budgets | 12 |
| 9 | Hash-chained WORM audit log, kill switch, OpenTelemetry, Langfuse, Grafana | 13, 14, 15, 19, 24-27 |
| 10 | GitHub Actions: policy validation, data-quality self-test, eval gate | CI |
| 11 | Azure profile (Container Apps, Entra ID, Key Vault, APIM, Blob WORM) | 28-31 |

## Data sources

- [SROIE](https://huggingface.co/datasets/jsdnrs/ICDAR2019-SROIE) and [CORD v2](https://huggingface.co/datasets/naver-clova-ix/cord-v2): real receipts with ground-truth labels, via Hugging Face `datasets`.
- [USAspending.gov](https://api.usaspending.gov/) `spending_by_award`: real contract recipients, amounts and dates for the vendor master.

## Acknowledgements

The governance scope of this project was inspired by the "Financial Agent with AI Governance" course project ([data-guru0/Financial-Agent-with-AI-Governance](https://github.com/data-guru0/Financial-Agent-with-AI-Governance)). Warden is an independent implementation with its own code, design and Azure profile.
