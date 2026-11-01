import { statusOf } from "@/lib/status";

export function Status({ status }: { status: string }) {
  const s = statusOf(status);
  return <span className={`status status-${s.tone}`}>{s.label}</span>;
}
