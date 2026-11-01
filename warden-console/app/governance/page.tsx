"use client";
import { useLive } from "@/lib/useLive";

type Health = {
  extraction_prompt: { source: string; version: string | null };
  rules: { routine_amount_threshold: number; confidence_threshold: number; min_prior_payments: number;
           ledger_currency: string; settlement_hold_seconds: number };
};

export default function GovernancePage() {
  const { data, error } = useLive<Health>("/api/backend/health", 10000);
  const mlflow = process.env.NEXT_PUBLIC_MLFLOW_URL ?? "http://localhost:5500";

  return (
    <>
      <header className="page-head">
        <h1>Governance</h1>
        <p className="lede">The rules the agents run under right now, read from the running backend.</p>
      </header>
      {error && <p className="notice notice-error">{error}</p>}
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
    </>
  );
}
