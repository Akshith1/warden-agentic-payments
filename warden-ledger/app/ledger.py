"""Double-entry posting. The dry-run and the real commit both call
apply_transaction(); the only difference is whether the caller commits or
rolls back afterwards, so the two paths cannot drift apart."""
from decimal import Decimal

from .models import TransactionRequest, ValidationResult


def apply_transaction(conn, req: TransactionRequest) -> ValidationResult:
    errors: list[str] = []

    if len(req.postings) < 2:
        errors.append("a transaction needs at least two postings")
    if any(p.amount <= 0 for p in req.postings):
        errors.append("posting amounts must be positive")

    debits = sum((p.amount for p in req.postings if p.direction == "debit"), Decimal("0"))
    credits = sum((p.amount for p in req.postings if p.direction == "credit"), Decimal("0"))
    if debits != credits:
        errors.append(f"unbalanced entry: debits {debits} != credits {credits}")

    if conn.execute("SELECT 1 FROM transactions WHERE idempotency_key = %s", (req.idempotency_key,)).fetchone():
        errors.append(f"duplicate idempotency key: {req.idempotency_key}")

    account_ids = sorted({p.account_id for p in req.postings})
    # Row locks in a fixed order so concurrent payments can't both pass the funds check.
    rows = conn.execute(
        "SELECT id, allow_negative, balance FROM accounts WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
        (account_ids,),
    ).fetchall()
    accounts = {r["id"]: r for r in rows}
    for missing in sorted(set(account_ids) - accounts.keys()):
        errors.append(f"unknown account: {missing}")

    resulting = {a: accounts[a]["balance"] for a in accounts}
    for p in req.postings:
        if p.account_id in resulting:
            resulting[p.account_id] += p.amount if p.direction == "debit" else -p.amount
    for acc_id, balance in resulting.items():
        if not accounts[acc_id]["allow_negative"] and balance < 0:
            errors.append(f"insufficient funds in {acc_id}: resulting balance {balance}")

    if errors:
        return ValidationResult(valid=False, errors=errors, resulting_balances=resulting)

    txn_id = conn.execute(
        "INSERT INTO transactions (idempotency_key, description) VALUES (%s, %s) RETURNING id",
        (req.idempotency_key, req.description),
    ).fetchone()["id"]
    for p in req.postings:
        conn.execute(
            "INSERT INTO postings (transaction_id, account_id, direction, amount) VALUES (%s,%s,%s,%s)",
            (txn_id, p.account_id, p.direction, p.amount),
        )
    for acc_id, balance in resulting.items():
        conn.execute("UPDATE accounts SET balance = %s WHERE id = %s", (balance, acc_id))

    return ValidationResult(valid=True, errors=[], resulting_balances=resulting, transaction_id=txn_id)
