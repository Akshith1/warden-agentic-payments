# Warden

**Governed agentic payments.** A team of AI agents reads invoices, scores them for fraud, decides whether they are safe to pay, and pays the safe ones through a real double-entry ledger, while governance layers decide what each agent is allowed to do and keep a record of every decision.

This repository is being built in phases. **Phase 1, the agent core, is complete:** four LLM agents and a deterministic payment executor run as one LangGraph workflow, on real invoice data, with human-in-the-loop approval and a real ledger. Each later phase adds one governance layer around the agents (see [Roadmap](#roadmap)).

## How a payment flows today

```
invoice text
   |
Extraction agent ........ typed fields: vendor, number, date, total, line items, remit-to account
   |                      (blocked if the vendor or total is missing, or line items don't add up)
Risk Scoring agent ...... fraud score against real vendor history from the ledger
   |                      (first-seen vendor, high risk, or a changed bank account -> human)
Approval agent .......... auto_approve / escalate / reject, with a confidence score
   |                      (over $5,000 or confidence < 0.75 -> human)
Critic agent ............ second opinion from a different provider; any concrete concern -> human
   |
Human review ............ LangGraph interrupt(), durable in Postgres; resume with approve/reject
   |
Payment execution ....... no LLM: dry run -> 20 s settlement hold -> double-entry commit
                          -> vendor history updated only after settlement
```

| Agent | Model route | Typed output |
| --- | --- | --- |
| Extraction | `warden-routine` (Groq `gpt-oss-120b`) | `InvoiceExtraction` |
| Risk Scoring | `warden-routine` up to $5,000, `warden-reasoning` (OpenAI) above | `RiskAssessment` |
| Approval | `warden-reasoning` | `ApprovalDecision` |
| Critic | `warden-routine`, deliberately the other provider from Approval | `CriticReview` |
| Payment Execution | none, by design | `payment` in the run state |

Agents never name a provider model. They call a route on the LiteLLM gateway, and every response is a Pydantic object validated by Instructor, which re-asks the model on a schema failure.

## Services

| Service | What it is | Local URL |
| --- | --- | --- |
| `warden-backend` | FastAPI + LangGraph agent runtime | http://localhost:8100/docs |
| `warden-ledger` | Double-entry ledger, vendor master, payment history | http://localhost:8101/docs |
| `litellm` | LLM gateway with the three routes | http://localhost:4000 |
| `postgres` | One database and role per service | localhost:15432 |
| `data-loader` | One-shot job: SROIE + CORD invoices, USAspending vendors | (job) |

## Run phase 1

You need Docker Desktop (about 4 GB of RAM is enough for phase 1), an OpenAI API key and a Groq API key.

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

## Tests

The tests run against a real Postgres and a real ledger process. Only the LLM is replaced by a test double, so routing and money movement are checked deterministically; model quality is the job of the eval gate in phase 2.

```sh
# Postgres with the service databases (for example the compose postgres on port 15432)
export POSTGRES_HOST=localhost POSTGRES_PORT=15432
export $(grep -E '^PGPASS_WARDEN_(LEDGER|BACKEND)=' .env | xargs)

cd warden-ledger && pip install -r requirements.txt -r requirements-dev.txt && pytest -q && cd ..
cd warden-backend && pip install -r requirements.txt -r requirements-dev.txt && pytest -q
```

They cover the happy path, first-seen vendors, human approve and reject, the $5,000 threshold and its route switch, low confidence, critic disagreement, a changed remit-to account, duplicate invoices, insufficient funds, missing totals, line items that don't add up, and the API's double-resume guard.

## Known simplifications in phase 1

These are deliberate and each one is removed by a named later phase.

- **Secrets live in `.env`.** Phase 6 moves the ledger write key into Infisical, fetched fresh before every payment.
- **One gateway key for all agents.** Phase 8 gives each agent its own key, route allowlist and daily budget.
- **Approval rules are enforced in code only.** Phase 4 adds Cedar as an independent second check, plus capability manifests.
- **Prompts are files in `warden-backend/app/prompts/`.** Phase 2 moves them to the MLflow registry behind a DeepEval promotion gate; the files stay as the fallback.
- **No guardrail model, OCR sandbox or kill switch yet.** Phases 5 and 9.
- **Single-currency ledger.** SROIE receipts are in Malaysian ringgit and CORD receipts in Indonesian rupiah. The ledger posts amounts at face value; the extracted currency is kept in the run state. CORD is used mainly as eval data for line items.

## Roadmap

| Phase | Adds | Proven by scenarios |
| --- | --- | --- |
| 1 | Agent core (this release) | 01 |
| 2 | MLflow prompt registry, DeepEval gate, vendor trust loop | 04, 05, 21 |
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
