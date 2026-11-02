"use client";
import { useLive } from "@/lib/useLive";

type Today = { agent: string; calls: number; tokens: number; cost_usd: number | null; daily_tokens: number; routes: string[] };
type Spend = { today: Today[] };

const usd = (v: number | null) => (v ? `$${v < 0.01 ? v.toFixed(4) : v.toFixed(2)}` : "no price known");

// Phase 8: what each agent may use at the gateway, and what it has used today (UTC).
export function AgentBudgets() {
  const { data, error } = useLive<Spend>("/api/control/spend", 10000);
  if (error && !data) return <p className="notice notice-error">{error}</p>;
  if (!data) return null;
  return (
    <>
      <h2 className="section">Model use by agent, today</h2>
      <p>
        Each agent calls the model gateway with its own key. The gateway refuses a route the agent isn&rsquo;t allowed,
        a paused agent, and an agent over its daily token budget; the run then fails and can be retried later. Budgets
        are in <code>infra/litellm/agents.yaml</code> and reset at midnight UTC.
      </p>
      <table className="runs budgets">
        <thead>
          <tr><th scope="col">Agent</th><th scope="col">Routes it may use</th><th scope="col" className="num">Calls</th>
              <th scope="col">Tokens used of daily budget</th><th scope="col" className="num">Cost</th></tr>
        </thead>
        <tbody>
          {data.today.map((a) => {
            const share = Math.min(1, a.tokens / a.daily_tokens);
            return (
              <tr key={a.agent}>
                <td><strong>{a.agent}</strong></td>
                <td className="small">{a.routes.join(", ")}</td>
                <td className="num">{a.calls}</td>
                <td>
                  <div className={`meter ${share >= 1 ? "meter-full" : share >= 0.8 ? "meter-high" : ""}`}
                       role="meter" aria-valuemin={0} aria-valuemax={a.daily_tokens} aria-valuenow={a.tokens}>
                    <span style={{ width: `${share * 100}%` }} />
                  </div>
                  <span className="small muted">{a.tokens.toLocaleString("en-US")} of {a.daily_tokens.toLocaleString("en-US")}</span>
                </td>
                <td className="num small">{usd(a.cost_usd)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </>
  );
}
