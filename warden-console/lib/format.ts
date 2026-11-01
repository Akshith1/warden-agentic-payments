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
  return `${s.toFixed(s < 10 ? 1 : 0)} s`;
}

// The backend's reasons are precise for engineers; the console says what they mean.
export function plainReason(reason: string | null | undefined): string | null {
  if (!reason) return null;
  if (reason.includes("duplicate idempotency key"))
    return "Already paid: this vendor's invoice with this number was settled before, so the ledger refused to pay it twice.";
  if (reason.includes("insufficient funds")) return "Not enough cash in the operating account to pay this.";
  return reason;
}
