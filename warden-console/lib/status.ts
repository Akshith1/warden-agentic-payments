// One vocabulary for run states across the console, in the words an AP controller uses.
export type Tone = "paid" | "needs" | "stopped" | "failed" | "working";

const STATUS: Record<string, { label: string; tone: Tone }> = {
  submitted: { label: "Received", tone: "working" },
  extracted: { label: "Reading", tone: "working" },
  scored: { label: "Scoring", tone: "working" },
  pending_human: { label: "Needs your decision", tone: "needs" },
  resuming: { label: "Resuming", tone: "working" },
  retrying: { label: "Retrying", tone: "working" },
  approved: { label: "Approved", tone: "working" },
  settling: { label: "In settlement hold", tone: "working" },
  settled: { label: "Paid", tone: "paid" },
  rejected: { label: "Rejected", tone: "stopped" },
  blocked: { label: "Stopped", tone: "stopped" },
  failed: { label: "Failed, can retry", tone: "failed" },
};

export function statusOf(s: string) {
  return STATUS[s] ?? { label: s, tone: "working" as Tone };
}

export const FINISHED = new Set(["settled", "rejected", "blocked", "failed"]);

export const STEP_LABEL: Record<string, string> = {
  extraction: "Extraction agent read the invoice",
  risk_scoring: "Risk agent scored it",
  approval: "Approval agent decided",
  critic: "Critic agent reviewed the decision",
  human_review: "A person decided",
  dry_run: "Ledger dry run",
  settlement_hold: "Settlement hold",
  commit: "Ledger commit",
};
