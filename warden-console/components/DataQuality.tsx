"use client";
import { useState } from "react";
import { clock } from "@/lib/format";
import { post, useLive } from "@/lib/useLive";

type Check = {
  id: string; suite: "ledger" | "vendors" | "inbox"; severity: "critical" | "warning"; title: string; why: string;
  status: "passed" | "failed" | "error"; count: number; examples?: string[]; error?: string;
};
type Result = {
  at: string; requested_by: string; duration_ms: number; great_expectations: string;
  passed: number; failed: number; errors: number; critical_failed: string[]; checks: Check[];
};

const SUITES: { id: Check["suite"]; name: string }[] = [
  { id: "ledger", name: "Ledger and vendor history" },
  { id: "vendors", name: "Reference vendors" },
  { id: "inbox", name: "Labelled receipts for the evaluation gate" },
];
const LABEL = { passed: "Passed", failed: "Failed", error: "Couldn't run" };
const STATUS_CLASS = { passed: "status-paid", failed: "status-stopped", error: "status-failed" };

function who(r: Result) {
  if (r.requested_by === "schedule") return "on its 10-minute schedule";
  if (r.requested_by === "service") return "by the data loader";
  return `by ${r.requested_by}`;
}

// Phase 7b: Great Expectations checks on the data money moves on. Critical failures stop payments.
export function DataQuality() {
  const { data, error, reload } = useLive<Result>("/api/quality/results", 15000);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  async function checkNow() {
    setBusy(true); setProblem(null);
    try { await post("/api/quality/run"); await reload(); }
    catch (e) { setProblem(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }

  return (
    <section id="data-quality">
      <h2 className="section">Data quality</h2>
      <p>
        Great Expectations checks the data payments depend on, every 10 minutes and after each data load. A failed{" "}
        <strong>critical</strong> check (the ledger, or vendor history) stops all payments through the kill switch;
        a failed <strong>warning</strong> is recorded and shown. The checks read through a database role that can
        only read, and report row ids, never personal values.
      </p>
      <p>
        <button className="button button-small" disabled={busy} onClick={checkNow}>
          {busy ? "Checking…" : "Check now"}
        </button>{" "}
        {data && (
          <span className="muted small">
            Last checked {clock(data.at)} {who(data)}: {data.passed} passed, {data.failed} failed
            {data.errors ? `, ${data.errors} couldn't run` : ""} ({(data.duration_ms / 1000).toFixed(1)} s,
            Great Expectations {data.great_expectations}).
          </span>
        )}
      </p>
      {problem && <p className="notice notice-error">{problem}</p>}
      {error && !data && (
        <p className={error.startsWith("no run yet") ? "muted" : "notice notice-error"}>
          {error.startsWith("no run yet") ? "No check has run yet. The first starts about 20 seconds after the service, or press Check now." : error}
        </p>
      )}
      {data && data.critical_failed.length > 0 && (
        <p className="notice notice-error">
          A critical check failed, so payments are stopped. Fix the data, press Check now, then a controller can
          resume payments. Resuming while it still fails stops them again at the next check.
        </p>
      )}
      {data && SUITES.map((suite) => (
        <table className="runs quality" key={suite.id}>
          <caption>{suite.name}</caption>
          <thead>
            <tr><th scope="col">Check</th><th scope="col">Kind</th><th scope="col">Result</th></tr>
          </thead>
          <tbody>
            {data.checks.filter((c) => c.suite === suite.id).map((c) => (
              <tr key={c.id}>
                <td>
                  {c.title}
                  <span className="muted small block">{c.why}</span>
                </td>
                <td className="small">{c.severity === "critical" ? <strong>Critical</strong> : "Warning"}</td>
                <td>
                  <span className={`status ${STATUS_CLASS[c.status]}`}>{LABEL[c.status]}</span>
                  {c.status === "failed" && (
                    <span className="small block">
                      {c.count} row{c.count === 1 ? "" : "s"}
                      {c.examples && c.examples.length > 0 && <>: <code>{c.examples.join(", ")}</code></>}
                    </span>
                  )}
                  {c.status === "error" && <span className="small block muted">{c.error}</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ))}
      <p className="muted small">
        Optional: the data catalogue (OpenMetadata) describes these tables, tags their personal data and the ones
        these checks guard, and shows where their rows come from. Start it with the <code>catalogue</code> profile,
        then open <a href={process.env.NEXT_PUBLIC_CATALOGUE_URL ?? "http://localhost:8585"} target="_blank"
        rel="noreferrer">the catalogue</a>.
      </p>
    </section>
  );
}
