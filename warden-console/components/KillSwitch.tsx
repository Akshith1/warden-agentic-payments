"use client";
import { useState } from "react";
import { clock } from "@/lib/format";
import { can, useMe } from "@/lib/me";
import type { ControlStatus } from "@/lib/types";
import { post, useLive } from "@/lib/useLive";

export function KillSwitch() {
  const me = useMe();
  const { data, error, reload } = useLive<ControlStatus>("/api/control/status", 3000);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  async function act(path: string, body: unknown) {
    setBusy(true); setProblem(null);
    try { await post(`/api/control/${path}`, body); setReason(""); await reload(); }
    catch (e) { setProblem(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }

  if (error && !data) return <p className="notice notice-error">{error}</p>;
  if (!data) return null;
  const stopped = data.global_stop && data.global;

  return (
    <section id="kill-switch" className={`kill ${stopped ? "kill-on" : ""}`}>
      <h2 className="section">Kill switch</h2>
      {stopped ? (
        <p>
          <strong>Payments are stopped.</strong> {data.global!.reason}. Stopped by {data.global!.by} at{" "}
          {clock(data.global!.since)}. Runs that reach a payment step fail with this reason and can be retried
          after payments resume. A payment in its settlement hold is stopped before money moves.
        </p>
      ) : (
        <p>
          Payments are running. Stopping them takes effect at once: the policy engine refuses every approval and
          ledger write, and any payment in its settlement hold is stopped before money moves. It also stops by
          itself when an invoice names a canary vendor or a critical data-quality check fails, and pauses an agent
          the policy engine keeps refusing.
        </p>
      )}

      {!stopped && can(me, "approver", "controller") && (
        <form className="kill-form" onSubmit={(e) => { e.preventDefault(); act("kill-switch/trip", { scope: "global", reason }); }}>
          <label>
            Why are you stopping payments?
            <input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="For example: checking a suspicious batch" />
          </label>
          <button className="button button-stop" disabled={busy || reason.trim().length < 3}>Stop all payments</button>
        </form>
      )}
      {stopped && can(me, "controller") && (
        <button className="button" disabled={busy} onClick={() => act("kill-switch/reset", { scope: "global" })}>
          Resume payments
        </button>
      )}
      {stopped && !can(me, "controller") && <p className="muted small">Only a controller can resume payments.</p>}

      {data.paused_agents.length > 0 && (
        <div className="paused">
          <h3>Paused agents</h3>
          <ul>
            {data.switches.filter((s) => s.scope === "agent").map((s) => (
              <li key={s.target}>
                <strong>{s.target}</strong>: {s.reason} (by {s.by}, {clock(s.since)}){" "}
                {can(me, "controller") && (
                  <button className="button button-quiet button-small" disabled={busy}
                    onClick={() => act("kill-switch/reset", { scope: "agent", target: s.target })}>Resume this agent</button>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
      {problem && <p className="notice notice-error">{problem}</p>}
      {!can(me, "approver", "controller") && <p className="muted small">Stopping payments needs the approver or controller role.</p>}
    </section>
  );
}
