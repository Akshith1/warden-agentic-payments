"use client";
import { AgentBudgets } from "@/components/AgentBudgets";
import { KillSwitch } from "@/components/KillSwitch";
import { useLive } from "@/lib/useLive";

type Policies = {
  cedar: { digest?: string; files?: string[]; error?: string;
           rules?: { id: string; effect: string; reason: string | null; summary: string | null; file: string }[] };
  capabilities: { agent: string; purpose: string; allowed_actions: string[] }[];
};

type Health = {
  extraction_prompt: { source: string; version: string | null };
  rules: { routine_amount_threshold: number; confidence_threshold: number; min_prior_payments: number;
           ledger_currency: string; settlement_hold_seconds: number };
};

const sentence = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

export default function GovernancePage() {
  const { data, error } = useLive<Health>("/api/backend/health", 10000);
  const { data: policies } = useLive<Policies>("/api/backend/policies", 30000);
  const mlflow = process.env.NEXT_PUBLIC_MLFLOW_URL ?? "http://localhost:5500";

  return (
    <>
      <header className="page-head">
        <h1>Governance</h1>
        <p className="lede">The rules the agents run under right now, read from the running backend.</p>
      </header>
      {error && <p className="notice notice-error">{error}</p>}
      <KillSwitch />
      <AgentBudgets />
      {data && (
        <>
          <h2 className="section">Extraction prompt</h2>
          <p>
            {data.extraction_prompt.source === "mlflow"
              ? <>Version {data.extraction_prompt.version}, promoted by the evaluation gate. </>
              : <>The bundled fallback: nothing is promoted in the registry, or it was unreachable at startup. </>}
            <a href={`${mlflow}/#/prompts/warden-extraction`} target="_blank" rel="noreferrer">See every version and its score in MLflow</a>
          </p>

          <h2 className="section">Rules enforced in code</h2>
          <dl className="rules">
            <div><dt>Goes to a person above</dt><dd>{data.rules.routine_amount_threshold.toLocaleString("en-US")} {data.rules.ledger_currency}</dd></div>
            <div><dt>Settled payments a vendor needs before auto-approval</dt><dd>{data.rules.min_prior_payments}</dd></div>
            <div><dt>Approval confidence needed to skip a person</dt><dd>{Math.round(data.rules.confidence_threshold * 100)}%</dd></div>
            <div><dt>Ledger currency (assumed when none is printed)</dt><dd>{data.rules.ledger_currency}</dd></div>
            <div><dt>Hold before money moves</dt><dd>{data.rules.settlement_hold_seconds} s</dd></div>
          </dl>
          <p className="muted small">Always sent to a person as well: first-seen vendors, a changed bank account, high risk scores, and any objection from the critic.</p>
        </>
      )}

      {policies && (
        <>
          <h2 className="section">Rules enforced by the policy engine</h2>
          <p>
            The same rules again, written as Cedar policies in the repository&rsquo;s <code>policies</code> folder and
            checked by a separate service before any payment. Both checks must agree; either one saying no sends the
            invoice to a person. The service also counts each vendor&rsquo;s payments over the last 24 hours, which the
            code rules do not.
          </p>
          {policies.cedar.error && <p className="notice notice-error">{policies.cedar.error}</p>}
          {policies.cedar.rules && (
            <table className="runs policy-table">
              <thead>
                <tr><th scope="col">Rule</th><th scope="col">What it does</th></tr>
              </thead>
              <tbody>
                {policies.cedar.rules.map((r) => (
                  <tr key={r.id}>
                    <td>
                      <code>{r.id}</code>
                      <span className="muted small block">
                        <span className={r.effect === "forbid" ? "effect-forbid" : "effect-permit"}>
                          {r.effect === "forbid" ? "Refuses" : "Allows"}
                        </span>{" "}· {r.file}
                      </span>
                    </td>
                    <td>
                      {sentence(r.summary ?? r.reason ?? "")}
                      {r.effect === "forbid" && r.summary && r.reason && (
                        <span className="muted small block">Approver is told: {r.reason}</span>
                      )}
                      {r.effect === "forbid" && !r.summary && <span className="muted small block">Refuses an agent; the run goes to a person with this reason</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {policies.cedar.digest && (
            <p className="muted small">Policy set {policies.cedar.digest}, validated when the policy service started.</p>
          )}

          <h2 className="section">Who may touch the ledger</h2>
          <p>
            Each role&rsquo;s capability manifest, checked in the backend&rsquo;s own code before any ledger call. This
            is the second lock on the ledger, independent of the Cedar policies above.
          </p>
          <dl className="rules">
            {policies.capabilities.map((m) => (
              <div key={m.agent}>
                <dt>{m.agent}<span className="small block">{m.purpose}</span></dt>
                <dd>{m.allowed_actions.length ? m.allowed_actions.join(", ") : "nothing"}</dd>
              </div>
            ))}
          </dl>
        </>
      )}
    </>
  );
}
