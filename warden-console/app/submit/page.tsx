"use client";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { money } from "@/lib/format";
import type { InboxRow } from "@/lib/types";
import { post, useLive } from "@/lib/useLive";

export default function SubmitPage() {
  const router = useRouter();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { data: inbox } = useLive<InboxRow[]>("/api/backend/inbox?limit=25", 60000);

  async function send(url: string, body: unknown, key: string) {
    setBusy(key);
    setError(null);
    try {
      const { run_id } = await post<{ run_id: string }>(url, body);
      router.push(`/runs/${run_id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(null);
    }
  }

  return (
    <>
      <header className="page-head">
        <h1>Submit an invoice</h1>
        <p className="lede">The agents read it, score it and decide. You'll watch each step on the next page.</p>
      </header>

      {error && <p className="notice notice-error">{error}</p>}

      <form className="paste" onSubmit={(e) => { e.preventDefault(); send("/api/backend/runs", { invoice_text: text, source: "paste" }, "paste"); }}>
        <label htmlFor="invoice">Paste the invoice text</label>
        <textarea id="invoice" rows={10} value={text} onChange={(e) => setText(e.target.value)}
          placeholder={"Vendor name and address\nInvoice number and date\nLine items\nTotal\nPayment details"} />
        <button className="button" disabled={!text.trim() || busy !== null}>
          {busy === "paste" ? "Submitting…" : "Submit invoice"}
        </button>
      </form>

      <section className="inbox">
        <h2>Or pick a real receipt</h2>
        <p className="muted">Held-out receipts from the SROIE dataset, loaded by the data loader. The label is what a human annotator wrote; the agents don't see it.</p>
        {inbox && inbox.length === 0 && <p className="muted">No receipts loaded. Run the data loader first.</p>}
        {inbox && inbox.length > 0 && (
          <table className="runs">
            <thead>
              <tr><th scope="col">Labelled vendor</th><th scope="col" className="num">Labelled total</th><th scope="col"><span className="visually-hidden">Action</span></th></tr>
            </thead>
            <tbody>
              {inbox.map((r) => (
                <tr key={r.external_key}>
                  <td>{r.labelled_vendor ?? <span className="muted">{r.external_key}</span>}</td>
                  <td className="num">{money(r.labelled_total?.replace(/[^0-9.]/g, "") || null)}</td>
                  <td className="num">
                    <button type="button" className="button button-quiet button-small" disabled={busy !== null}
                      onClick={() => send(`/api/backend/inbox/${r.external_key}/submit`, undefined, r.external_key)}>
                      {busy === r.external_key ? "Submitting…" : "Submit"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </>
  );
}
