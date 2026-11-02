export function money(value: string | number | null | undefined, currency?: string | null): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = typeof value === "number" ? value : Number(value);
  if (Number.isNaN(n)) return String(value);
  const formatted = n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return currency ? `${currency} ${formatted}` : formatted;
}

export function when(iso: string | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

export function clock(iso: string): string {
  return new Date(iso).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", second: "2-digit" });
}

export function seconds(from: string, to: string): string {
  const s = (new Date(to).getTime() - new Date(from).getTime()) / 1000;
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)} s`;
  // Waits for a human run to minutes or hours; say so in units a person reads at a glance.
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ${Math.round(s % 60)} s`;
  return `${Math.floor(m / 60)} h ${m % 60} min`;
}

// The backend's reasons are precise for engineers; the console says what they mean.
export function plainReason(reason: string | null | undefined): string | null {
  if (!reason) return null;
  if (reason.includes("duplicate idempotency key"))
    return "Already paid: this vendor's invoice with this number was settled before, so the ledger refused to pay it twice.";
  if (reason.includes("insufficient funds")) return "Not enough cash in the operating account to pay this.";
  return reason;
}

// One line for the Runs list. Provider errors are pages of JSON; the run page keeps the full text.
export function shortReason(reason: string | null | undefined): string | null {
  const plain = plainReason(reason);
  if (plain?.startsWith("kill switch:")) return "Stopped by the kill switch. Retry once a controller resumes payments.";
  if (!plain || !plain.startsWith("error:")) return plain;
  const code = plain.match(/Error code: (\d{3})/)?.[1];
  if (/no credits remaining|insufficient_quota/i.test(plain)) return "Model provider: out of API credit. Add credit, then retry.";
  if (code === "429") return "Model provider: rate limit reached. Wait, then retry.";
  if (code) return `Model provider refused the request (${code}). Open the run for details.`;
  return plain.length > 120 ? `${plain.slice(0, 117)}…` : plain;
}
