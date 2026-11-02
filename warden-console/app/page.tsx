"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Status } from "@/components/Status";
import { money, plainReason, shortReason, when } from "@/lib/format";
import type { RunRow } from "@/lib/types";
import { useLive } from "@/lib/useLive";

export default function RunsPage() {
  const { data: runs, error } = useLive<RunRow[]>("/api/backend/runs?limit=100", 2000);
  const router = useRouter();
  const waiting = runs?.filter((r) => r.status === "pending_human") ?? [];

  return (
    <>
      <header className="page-head">
        <h1>Runs</h1>
        <p className="lede">Every invoice the agents have handled, newest first. Updates by itself.</p>
      </header>

      {error && <p className="notice notice-error">{error}</p>}

      {waiting.length > 0 && (
        <Link href={`/runs/${waiting[waiting.length - 1].run_id}`} className="queue">
          <strong>{waiting.length === 1 ? "1 invoice needs your decision" : `${waiting.length} invoices need your decision`}</strong>
          <span>Review the oldest</span>
        </Link>
      )}

      {runs && runs.length === 0 && (
        <div className="empty">
          <p>No invoices yet.</p>
          <Link href="/submit" className="button">Submit an invoice</Link>
        </div>
      )}

      {runs && runs.length > 0 && (
        <table className="runs">
          <thead>
            <tr>
              <th scope="col">Vendor</th>
              <th scope="col" className="num">Amount</th>
              <th scope="col">Status</th>
              <th scope="col">Why</th>
              <th scope="col">Updated</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((r) => (
              // The whole row opens the run; the vendor link stays for keyboard and screen readers.
              <tr key={r.run_id} className="clickable" onClick={() => router.push(`/runs/${r.run_id}`)}>
                <td>
                  <Link href={`/runs/${r.run_id}`} className="row-link">
                    {r.vendor_name ?? <span className="muted">Not read yet</span>}
                  </Link>
                </td>
                <td className="num">{money(r.total_amount)}</td>
                <td><Status status={r.status} /></td>
                <td className="why" title={plainReason(r.status_reason) ?? undefined}>{shortReason(r.status_reason) ?? ""}</td>
                <td className="muted nowrap">{when(r.updated_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
