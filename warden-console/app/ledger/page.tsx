"use client";
import { money, when } from "@/lib/format";
import { useLive } from "@/lib/useLive";

type Account = { id: string; name: string; type: string; balance: string };
type Txn = { id: number; description: string; created_at: string; postings: { account_id: string; direction: string; amount: string }[] };

export default function LedgerPage() {
  const { data: accounts, error } = useLive<Account[]>("/api/ledger/accounts", 5000);
  const { data: txns } = useLive<Txn[]>("/api/ledger/transactions?limit=25", 5000);
  const cash = accounts?.find((a) => a.id === "1000-operating-cash");

  return (
    <>
      <header className="page-head">
        <h1>Ledger</h1>
        <p className="lede">The double-entry books payments are posted to. Every entry balances; nothing here is edited, only added.</p>
      </header>
      {error && <p className="notice notice-error">{error}</p>}

      {cash && (
        <p className="cash"><span className="muted">Operating cash</span> {money(cash.balance)}</p>
      )}

      {accounts && (
        <table className="runs">
          <thead><tr><th scope="col">Account</th><th scope="col">Type</th><th scope="col" className="num">Balance</th></tr></thead>
          <tbody>
            {accounts.map((a) => (
              <tr key={a.id}><td>{a.name} <span className="muted">{a.id}</span></td><td>{a.type}</td><td className="num">{money(a.balance)}</td></tr>
            ))}
          </tbody>
        </table>
      )}

      <h2 className="section">Recent entries</h2>
      {txns && txns.length === 0 && <p className="muted">No entries yet.</p>}
      {txns && txns.length > 0 && (
        <table className="runs">
          <thead><tr><th scope="col">Entry</th><th scope="col">Debit</th><th scope="col">Credit</th><th scope="col" className="num">Amount</th><th scope="col">Posted</th></tr></thead>
          <tbody>
            {txns.map((t) => {
              const debit = t.postings.find((p) => p.direction === "debit");
              const credit = t.postings.find((p) => p.direction === "credit");
              return (
                <tr key={t.id}>
                  <td>{t.description}</td>
                  <td className="muted">{debit?.account_id}</td>
                  <td className="muted">{credit?.account_id}</td>
                  <td className="num">{money(debit?.amount ?? null)}</td>
                  <td className="muted nowrap">{when(t.created_at)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </>
  );
}
