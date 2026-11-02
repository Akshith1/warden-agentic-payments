"use client";
import Link from "next/link";
import { when } from "@/lib/format";
import { STEP_LABEL } from "@/lib/status";
import type { AuditEntry, ChainCheck } from "@/lib/types";
import { useLive } from "@/lib/useLive";

function summary(e: AuditEntry): string {
  const p = e.payload as Record<string, unknown>;
  switch (e.type) {
    case "run_submitted": return `Submitted (${p.source})`;
    case "run_step": {
      const label = STEP_LABEL[String(p.step)] ?? String(p.step);
      const extra = p.decision ? `: ${p.decision}` : p.outcome ? `: ${p.outcome}` : p.ok === false ? ": refused" : "";
      return `${label}${extra}${p.vendor ? ` · ${p.vendor}` : ""}`;
    }
    case "approval_refused": return `Approval refused: ${(p.reasons as string[]).join("; ")}`;
    case "run_stopped": return `Stopped: ${p.reason}`;
    case "kill_switch_tripped": return `${p.automatic ? "Automatic stop" : "Stopped"} (${p.scope === "agent" ? `agent ${p.target}` : "all payments"}): ${p.reason}`;
    case "kill_switch_reset": return `Resumed (${p.scope === "agent" ? `agent ${p.target}` : "all payments"})`;
    default: return e.type;
  }
}

export default function AuditPage() {
  const { data: check } = useLive<ChainCheck>("/api/control/audit/verify", 10000);
  const { data: entries, error } = useLive<AuditEntry[]>("/api/control/audit?limit=200", 5000);

  return (
    <>
      <header className="page-head">
        <h1>Audit log</h1>
        <p className="lede">Every step of every run, every refusal and every stop, in order. Entries can be added, never changed.</p>
      </header>
      {check && (check.valid ? (
        <p className="notice chain-ok">
          <strong>Chain verified.</strong> {check.entries} entries, each sealed with the fingerprint of the one before.
          Latest fingerprint <code>{check.head?.slice(0, 16)}</code>. Write it down; if any earlier entry is ever edited,
          this page stops showing it.
        </p>
      ) : (
        <p className="notice notice-error chain-broken" role="alert">
          <strong>The chain is broken at entry {check.broken_at}:</strong> {check.problem}. Entries 1 to {check.entries} are
          intact; everything from entry {check.broken_at} on can't be trusted.
        </p>
      ))}
      {error && <p className="notice notice-error">{error}</p>}
      {entries && (
        <table className="runs audit">
          <thead>
            <tr><th scope="col" className="num">#</th><th scope="col">When</th><th scope="col">What</th><th scope="col">Who</th><th scope="col">Run</th><th scope="col">Fingerprint</th></tr>
          </thead>
          <tbody>
            {entries.map((e) => (
              <tr key={e.seq} className={check && !check.valid && e.seq >= (check.broken_at ?? Infinity) ? "suspect" : ""}>
                <td className="num">{e.seq}</td>
                <td className="nowrap muted">{when(e.at)}</td>
                <td>{summary(e)}</td>
                <td>{e.actor ?? ""}</td>
                <td>{e.run_id ? <Link href={`/runs/${e.run_id}`}>{e.run_id.slice(0, 8)}</Link> : ""}</td>
                <td><code className="hash">{e.hash.slice(0, 10)}</code></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
