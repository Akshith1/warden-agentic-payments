"""Integration tests: the real LangGraph graph, the real Postgres checkpointer
and a real warden-ledger process over HTTP. Only the LLM is replaced, by a
test double that returns fixed typed objects, so the routing and money paths
are tested deterministically. Model quality is the eval gate's job (phase 2).

Needs: a Postgres with the warden_ledger and warden_backend databases
(infra/postgres-init), and env PGPASS_WARDEN_LEDGER, PGPASS_WARDEN_BACKEND,
POSTGRES_HOST, POSTGRES_PORT."""
import uuid

import httpx
from langgraph.types import Command

from app import config
from app.agents import Deps
from app.graph import build_graph
from app.schemas import ApprovalDecision, CriticReview, InvoiceExtraction, RiskAssessment


def cash(ledger_url):
    rows = httpx.get(f"{ledger_url}/accounts", headers={"X-API-Key": "t-read"}).json()
    return float(next(r["balance"] for r in rows if r["id"] == "1000-operating-cash"))


def seed_history(ledger, vendor, amounts, remit="ACCT-111"):
    for i, a in enumerate(amounts):
        ledger.record_payment({"vendor_name": vendor, "amount": f"{a:.2f}", "paid_on": f"2026-0{i + 1}-15",
                               "external_ref": f"seed-{vendor}-{i}", "remit_account": remit})


class FakeLLM:
    """Test double for app.llm.structured. Records which route each agent used."""

    def __init__(self, extraction, risk="low", decision="auto_approve", confidence=0.95, critic_agrees=True):
        self.extraction, self.risk, self.decision = extraction, risk, decision
        self.confidence, self.critic_agrees = confidence, critic_agrees
        self.calls = []

    def __call__(self, agent, route, system, user, model):
        self.calls.append((agent, route))
        if model is InvoiceExtraction:
            return InvoiceExtraction(**self.extraction)
        if model is RiskAssessment:
            score = {"low": 0.1, "medium": 0.5, "high": 0.9}[self.risk]
            return RiskAssessment(risk_level=self.risk, risk_score=score, signals=[], rationale="test")
        if model is ApprovalDecision:
            return ApprovalDecision(decision=self.decision, confidence=self.confidence, reasons=["test"])
        if model is CriticReview:
            return CriticReview(agrees=self.critic_agrees, concerns=[] if self.critic_agrees else ["mismatch"])
        raise AssertionError(model)


def invoice(vendor="Acme Supply", number=None, total=120.0, remit="ACCT-111", items=None):
    return {"vendor_name": vendor, "invoice_number": number or uuid.uuid4().hex[:8], "invoice_date": "2026-10-01",
            "total_amount": total, "line_items": items or [], "remit_to_account": remit,
            "extraction_confidence": 0.95}


def run(checkpointer, ledger, fake, run_id=None):
    graph = build_graph(Deps(llm=fake, ledger=ledger, sleep=lambda s: None), checkpointer)
    run_id = run_id or str(uuid.uuid4())
    cfg = {"configurable": {"thread_id": run_id}}
    graph.invoke({"run_id": run_id, "invoice_text": "raw text", "source": "paste", "audit_trail": []}, cfg)
    return graph, cfg


def state(graph, cfg):
    return graph.get_state(cfg).values


def test_known_vendor_routine_amount_settles(checkpointer, ledger, ledger_url):
    seed_history(ledger, "Acme Supply", [100, 120, 110])
    fake = FakeLLM(invoice(total=115.0))
    graph, cfg = run(checkpointer, ledger, fake)
    s = state(graph, cfg)
    assert s["status"] == "settled"
    assert cash(ledger_url) == 10000 - 115
    assert [a for a, _ in fake.calls] == ["extraction", "risk-scoring", "approval", "critic"]
    assert dict(fake.calls)["critic"] != dict(fake.calls)["approval"]  # critic on the other provider's route
    assert ledger.lookup_vendor("ACME SUPPLY")["payment_count"] == 4  # history grows only after settlement
    assert [e["step"] for e in s["audit_trail"]][-3:] == ["dry_run", "settlement_hold", "commit"]


def test_first_seen_vendor_waits_for_human_then_settles(checkpointer, ledger, ledger_url):
    graph, cfg = run(checkpointer, ledger, FakeLLM(invoice(vendor="Brand New LLC", total=90.0)))
    snap = graph.get_state(cfg)
    assert snap.values["status"] == "pending_human" and snap.interrupts
    assert "first-seen vendor" in snap.values["escalation_reasons"]
    assert cash(ledger_url) == 10000

    graph.invoke(Command(resume={"decision": "approve", "approver": "controller-1"}), cfg)
    assert state(graph, cfg)["status"] == "settled"
    assert cash(ledger_url) == 10000 - 90


def test_human_reject_moves_no_money(checkpointer, ledger, ledger_url):
    graph, cfg = run(checkpointer, ledger, FakeLLM(invoice(vendor="Brand New LLC")))
    graph.invoke(Command(resume={"decision": "reject", "approver": "controller-1"}), cfg)
    s = state(graph, cfg)
    assert s["status"] == "rejected" and cash(ledger_url) == 10000


def test_amount_over_threshold_escalates_and_uses_reasoning_route(checkpointer, ledger):
    seed_history(ledger, "Acme Supply", [6000, 6100])
    fake = FakeLLM(invoice(total=6050.0))
    graph, cfg = run(checkpointer, ledger, fake)
    s = state(graph, cfg)
    assert s["status"] == "pending_human"
    assert any("above $5,000" in r for r in s["escalation_reasons"])
    assert dict(fake.calls)["risk-scoring"] == config.ROUTE_REASONING


def test_low_confidence_escalates(checkpointer, ledger):
    seed_history(ledger, "Acme Supply", [100])
    graph, cfg = run(checkpointer, ledger, FakeLLM(invoice(), confidence=0.6))
    s = state(graph, cfg)
    assert s["status"] == "pending_human" and any("confidence 0.60" in r for r in s["escalation_reasons"])


def test_critic_disagreement_escalates(checkpointer, ledger):
    seed_history(ledger, "Acme Supply", [100])
    graph, cfg = run(checkpointer, ledger, FakeLLM(invoice(), critic_agrees=False))
    s = state(graph, cfg)
    assert s["status"] == "pending_human" and s["escalation_reasons"] == ["critic disagreed: mismatch"]


def test_changed_remit_account_escalates(checkpointer, ledger):
    seed_history(ledger, "Acme Supply", [100, 110], remit="ACCT-111")
    graph, cfg = run(checkpointer, ledger, FakeLLM(invoice(remit="NEW-999")))
    s = state(graph, cfg)
    assert s["status"] == "pending_human"
    assert any("remit-to account differs" in r for r in s["escalation_reasons"])


def test_agent_reject_ends_run(checkpointer, ledger, ledger_url):
    seed_history(ledger, "Acme Supply", [100])
    graph, cfg = run(checkpointer, ledger, FakeLLM(invoice(), decision="reject"))
    assert state(graph, cfg)["status"] == "rejected" and cash(ledger_url) == 10000


def test_duplicate_invoice_blocked_by_ledger(checkpointer, ledger, ledger_url):
    seed_history(ledger, "Acme Supply", [100])
    graph, cfg = run(checkpointer, ledger, FakeLLM(invoice(number="INV-7", total=100.0)))
    assert state(graph, cfg)["status"] == "settled"
    graph, cfg = run(checkpointer, ledger, FakeLLM(invoice(number="inv 7", total=100.0)))
    s = state(graph, cfg)
    assert s["status"] == "blocked" and "duplicate idempotency key" in s["status_reason"]
    assert cash(ledger_url) == 10000 - 100


def test_insufficient_funds_blocked_by_dry_run(checkpointer, ledger, ledger_url):
    graph, cfg = run(checkpointer, ledger, FakeLLM(invoice(vendor="Big Order Co", total=12000.0)))
    graph.invoke(Command(resume={"decision": "approve", "approver": "cfo"}), cfg)
    s = state(graph, cfg)
    assert s["status"] == "blocked" and "insufficient funds" in s["status_reason"]
    assert cash(ledger_url) == 10000


def test_missing_total_requests_documentation(checkpointer, ledger):
    bad = invoice() | {"total_amount": None}
    fake = FakeLLM(bad)
    graph, cfg = run(checkpointer, ledger, fake)
    s = state(graph, cfg)
    assert s["status"] == "blocked" and "total amount not found" in s["status_reason"]
    assert [a for a, _ in fake.calls] == ["extraction"]


def test_line_items_that_do_not_add_up(checkpointer, ledger):
    items = [{"description": "a", "amount": 50.0}, {"description": "b", "amount": 40.0}]
    graph, cfg = run(checkpointer, ledger, FakeLLM(invoice(total=120.0, items=items)))
    assert "line items sum to 90.00" in state(graph, cfg)["status_reason"]
