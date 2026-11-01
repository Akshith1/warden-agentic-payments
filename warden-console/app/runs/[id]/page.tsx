"use client";
import Link from "next/link";
import { use, useState } from "react";
import { Status } from "@/components/Status";
import { clock, money, plainReason, seconds } from "@/lib/format";
import { FINISHED, STEP_LABEL, statusOf } from "@/lib/status";
import type { RunDetail, TrailEntry } from "@/lib/types";
import { post, useLive } from "@/lib/useLive";

export default function RunPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { data, error, reload } = useLive<RunDetail>(`/api/backend/runs/${id}`, 1500);

  if (error && !data) return <p className="notice notice-error">{error}</p>;
  if (!data) return <p className="muted">Loading run…</p>;

  const status = data.run?.status ?? data.state.status;

  const { state } = data;
  const ext = state.extraction;
  const reason = data.run?.status_reason ?? state.status_reason;

  return (
    <>
      <p className="crumb"><Link href="/">Runs</Link></p>

      <div className="run">
        <article className="voucher" aria-label="Invoice as the agents read it">
          <div className="voucher-head">
            <div>
              <h1 className="vendor">{ext?.vendor_name ?? "Reading the invoice…"}</h1>
              <p className="muted">
                {ext?.invoice_number ? `Invoice ${ext.invoice_number}` : "No invoice number"}
                {ext?.invoice_date ? `, dated ${ext.invoice_date}` : ""}
              </p>
            </div>
            <Status status={status} />
          </div>

          <p className="amount">
            <span className="currency">
              {ext?.currency ?? ""}
              {ext?.currency_assumed ? <abbr title="No currency printed; ledger currency assumed by rule">*</abbr> : null}
            </span>
            {money(ext?.total_amount)}
          </p>

          {ext && ext.line_items.length > 0 && (
            <table className="lines">
              <tbody>
                {ext.line_items.map((li, i) => (
                  <tr key={i}>
                    <td>{li.description}{li.quantity ? <span className="muted"> × {li.quantity}</span> : null}</td>
                    <td className="num">{money(li.amount)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <dl className="facts">
            <div><dt>Pay to</dt><dd>{ext?.remit_to_account ?? "Not stated"}</dd></div>
            <div>
              <dt>Vendor history</dt>
              <dd>
                {state.vendor
                  ? state.vendor.known
                    ? `${state.vendor.payment_count} settled payments, average ${money(state.vendor.average_amount)}`
                    : "Never paid before"
                  : "Not checked yet"}
              </dd>
            </div>
            {ext && <div><dt>Read with confidence</dt><dd>{Math.round(ext.extraction_confidence * 100)}%</dd></div>}
          </dl>

          {status === "pending_human" && <Decide id={id} reasons={state.escalation_reasons ?? []} onDone={reload} />}
          {status === "failed" && <Retry id={id} reason={reason} onDone={reload} />}
          {reason && status !== "failed" && status !== "pending_human" && <p className="notice">{plainReason(reason)}</p>}

          <details className="source">
            <summary>Invoice text as received</summary>
            <pre>{state.invoice_text}</pre>
          </details>
        </article>

        <section className="trail" aria-label="What each agent did">
          <h2>Trail</h2>
          <ol>
            {state.audit_trail.map((e, i) => (
              <Step key={`${e.step}-${e.at}`} entry={e} previous={state.audit_trail[i - 1]} />
            ))}
          </ol>
          {FINISHED.has(status) && (
            <p className={`stamp stamp-${statusOf(status).tone}`}>{statusOf(status).label}</p>
          )}
          {!FINISHED.has(status) && status !== "pending_human" && <p className="working">Working…</p>}
        </section>
      </div>
    </>
  );
}

function Step({ entry, previous }: { entry: TrailEntry; previous?: TrailEntry }) {
  const e = entry as Record<string, unknown>;
  let detail: React.ReactNode = null;
  switch (entry.step) {
    case "extraction":
      detail = e.outcome === "needs_documentation"
        ? `Asked for documentation: ${(e.problems as string[]).join("; ")}`
        : e.prompt_source === "mlflow" ? `Prompt v${e.prompt_version}, promoted by the evaluation gate` : "Bundled prompt (nothing promoted in the registry)";
      break;
    case "risk_scoring":
      detail = `${e.risk_level} risk (${Number(e.risk_score).toFixed(2)})${
        (e.escalation as string[])?.length ? `, sent to a person: ${(e.escalation as string[]).join("; ")}` : ""}`;
      break;
    case "approval":
      detail = `${String(e.decision).replace("_", " ")}, ${Math.round(Number(e.confidence) * 100)}% sure`;
      break;
    case "critic":
      detail = e.agrees ? "Agreed" : `Objected: ${(e.concerns as string[]).join("; ")}`;
      break;
    case "human_review":
      detail = `${e.decision === "approve" ? "Approved" : "Rejected"} by ${e.approver}${e.note ? `: ${e.note}` : ""}`;
      break;
    case "dry_run":
      detail = e.valid ? "Would balance; nothing written" : `Refused: ${(e.errors as string[]).join("; ")}`;
      break;
    case "settlement_hold":
      detail = `${e.seconds} s to stop it before money moves`;
      break;
    case "commit":
      detail = e.ok ? `Posted as transaction ${e.transaction_id}` : `Refused: ${(e.errors as string[]).join("; ")}`;
      break;
  }
  return (
    <li>
      <span className="step-name">{STEP_LABEL[entry.step] ?? entry.step}</span>
      <span className="step-time">
        {clock(entry.at)}
        {previous ? <span className="muted"> +{seconds(previous.at, entry.at)}</span> : null}
      </span>
      {detail && <span className="step-detail">{detail}</span>}
    </li>
  );
}

function Decide({ id, reasons, onDone }: { id: string; reasons: string[]; onDone: () => void }) {
  const [approver, setApprover] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function decide(decision: "approve" | "reject") {
    setBusy(decision);
    setError(null);
    try {
      await post(`/api/backend/runs/${id}/resume`, { decision, approver: approver.trim(), note });
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(null);
    }
  }

  return (
    <form className="decide" onSubmit={(ev) => ev.preventDefault()}>
      <h2>Your decision</h2>
      <p>The agents sent this to a person because:</p>
      <ul>{reasons.map((r) => <li key={r}>{r}</li>)}</ul>
      <label>
        Your name
        <input value={approver} onChange={(e) => setApprover(e.target.value)} autoComplete="name" required />
      </label>
      <label>
        <span>Note <span className="muted">(optional)</span></span>
        <input value={note} onChange={(e) => setNote(e.target.value)} />
      </label>
      {error && <p className="notice notice-error">{error}</p>}
      <div className="actions">
        <button type="button" className="button button-pay" disabled={!approver.trim() || busy !== null}
          onClick={() => decide("approve")}>
          {busy === "approve" ? "Approving…" : "Approve payment"}
        </button>
        <button type="button" className="button button-quiet" disabled={!approver.trim() || busy !== null}
          onClick={() => decide("reject")}>
          {busy === "reject" ? "Rejecting…" : "Reject"}
        </button>
      </div>
      <p className="muted small">Approving starts the dry run and the settlement hold. Phase 6 takes your name from your login.</p>
    </form>
  );
}

function Retry({ id, reason, onDone }: { id: string; reason?: string | null; onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <div className="decide">
      <h2>This run stopped on a provider error</h2>
      <p className="why-long">{reason}</p>
      <p>Fix the cause (for example add API credit or wait for a rate limit), then continue. Finished steps are not repeated.</p>
      {error && <p className="notice notice-error">{error}</p>}
      <button type="button" className="button" disabled={busy} onClick={async () => {
        setBusy(true);
        try { await post(`/api/backend/runs/${id}/retry`); onDone(); }
        catch (e) { setError(e instanceof Error ? e.message : String(e)); setBusy(false); }
      }}>
        {busy ? "Retrying…" : "Retry from the failed step"}
      </button>
    </div>
  );
}
