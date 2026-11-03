"""Phase 9b: what the backend tells Grafana, checked on real runs of the real
graph (the model is the usual test double), and the OTLP export checked against
a stand-in collector that decodes what it receives."""
import json
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from langgraph.types import Command
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import llm, telemetry
from app.schemas import RiskAssessment

from .test_graph import FakeLLM, invoice, run, seed_history

SPANS, METRICS, LOGS = InMemorySpanExporter(), InMemoryMetricReader(), InMemoryLogRecordExporter()


@pytest.fixture(scope="module", autouse=True)
def telemetry_on():
    assert telemetry.configure(span_exporter=SPANS, metric_readers=[METRICS], log_exporter=LOGS)


@pytest.fixture(autouse=True)
def fresh():
    SPANS.clear()
    LOGS.clear()


def points(name: str) -> list:
    data = METRICS.get_metrics_data()
    return [p for rm in data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics
            if m.name == name for p in m.data.data_points]


def step_lines() -> list[dict]:
    return [json.loads(r.log_record.body) for r in LOGS.get_finished_logs()]


def by_name(name: str):
    return [s for s in SPANS.get_finished_spans() if s.name == name]


def test_every_step_of_a_paid_invoice_is_a_span_with_what_it_decided(checkpointer, ledger):
    seed_history(ledger, "Telemetry Supply", [100, 120, 110])
    run_id = str(uuid.uuid4())
    with telemetry.run_span(run_id, "submit") as root:
        graph, cfg = run(checkpointer, ledger, FakeLLM(invoice(vendor="Telemetry Supply", total=115.0)), run_id)
        telemetry.run_ended(root, graph.get_state(cfg).values)

    names = [s.name for s in SPANS.get_finished_spans()]
    for step in ("privacy", "guardrail", "extraction", "risk_scoring", "approval", "critic", "policy_check",
                 "payment_execution", "settle"):
        assert f"step {step}" in names
    run_span = by_name("invoice run (submit)")[0]
    assert run_span.attributes["warden.status"] == "settled"
    assert run_span.attributes["warden.vendor"] == "Telemetry Supply"
    # Every step is a child of the run, in the same trace.
    assert {s.context.trace_id for s in SPANS.get_finished_spans()} == {run_span.context.trace_id}
    risk = by_name("step risk_scoring")[0].attributes
    assert risk["warden.risk_score"] == 0.1 and risk["warden.risk_level"] == "low"
    assert by_name("step approval")[0].attributes["warden.confidence"] == 0.95
    assert by_name("step policy_check")[0].attributes["warden.decision"] == "Allow"

    assert any(p.attributes == {"outcome": "settled"} for p in points("warden.runs"))
    assert any(p.attributes["step"] == "risk_scoring" and p.count >= 1 for p in points("warden.agent.duration"))
    assert any(p.sum >= 0.1 for p in points("warden.risk.score"))
    assert any(p.attributes == {"decision": "Allow", "principal": "Agent"} for p in points("warden.policy.decisions"))

    lines = [line for line in step_lines() if line["run_id"] == run_id]
    risk_line = next(line for line in lines if line["step"] == "risk_scoring")
    assert risk_line["vendor"] == "Telemetry Supply" and risk_line["risk_score"] == 0.1


def test_a_pause_for_a_person_is_not_an_error_and_their_note_stays_out(checkpointer, ledger):
    run_id = str(uuid.uuid4())
    graph, cfg = run(checkpointer, ledger, FakeLLM(invoice(vendor="Paused Person LLC", total=40.0)), run_id)
    review = by_name("step human_review")[0]
    assert review.attributes["warden.paused"] is True and review.status.is_ok
    assert any(p.attributes == {"kind": "first_seen"} for p in points("warden.escalations"))

    graph.invoke(Command(resume={"decision": "approve", "approver": "amira", "roles": ["approver"],
                                 "note": "Called Siti on 012-345 6789 to confirm"}), cfg)
    exported = json.dumps([dict(s.attributes) for s in SPANS.get_finished_spans()]) + json.dumps(step_lines())
    assert "Siti" not in exported and "012-345" not in exported
    assert any(line.get("approver") == "amira" for line in step_lines())


def test_invoice_text_and_masked_values_never_leave(checkpointer, ledger):
    # The PII stand-in masks the cashier; nothing from the raw text may reach telemetry.
    run_id = str(uuid.uuid4())
    graph, cfg = run(checkpointer, ledger, FakeLLM(invoice(vendor="Privacy Check Sdn Bhd", total=30.0)), run_id)
    graph.update_state(cfg, {"invoice_text": "Cashier: NUR AISYAH\nraw text"})
    exported = json.dumps([dict(s.attributes) for s in SPANS.get_finished_spans()]) + json.dumps(step_lines())
    assert "raw text" not in exported and "NUR AISYAH" not in exported


def test_a_model_call_reports_its_route_tokens_and_time(monkeypatch):
    completion = SimpleNamespace(model="openai/gpt-oss-120b", usage=SimpleNamespace(prompt_tokens=812,
                                                                                    completion_tokens=96))
    answer = RiskAssessment(risk_level="medium", risk_score=0.6, signals=[], rationale="x")
    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create_with_completion=lambda **kw: (answer, completion))))
    monkeypatch.setattr(llm, "_client", lambda agent: fake)
    assert llm.structured("risk-scoring", "warden-routine", "sys", "user", RiskAssessment) is answer
    span = by_name("chat warden-routine")[0]
    assert span.attributes["gen_ai.usage.input_tokens"] == 812
    assert span.attributes["gen_ai.response.model"] == "openai/gpt-oss-120b"
    assert span.attributes["warden.agent"] == "risk-scoring"
    tokens = {p.attributes["kind"]: p.value for p in points("warden.llm.tokens")
              if p.attributes["agent"] == "risk-scoring"}
    assert tokens["input"] >= 812 and tokens["output"] >= 96


def test_escalation_reasons_are_grouped_into_kinds():
    assert telemetry.reason_kind("limited history: 1 prior payment(s), auto-approval needs 2") == "limited_history"
    assert telemetry.reason_kind("remit-to account differs from the vendor's last settled payment") == \
        "remit_changed"
    assert telemetry.reason_kind("something new") == "other"
    assert telemetry.reason_kind("approval agent escalated: not unanimous: 2 of 3 asks said auto_approve") == \
        "approval_not_unanimous"
    assert telemetry.reason_kind("approval agent escalated: amount looks unusual") == "approval_agent"
    assert telemetry.reason_kind("critic disagreed: total mismatch") == "critic"
    assert telemetry.reason_kind("policy: the vendor already had 3 payments") == "policy"
    assert telemetry.reason_kind("decision confidence 0.70 below 0.75") == "low_confidence"


def test_reasons_raised_after_the_risk_step_are_counted(checkpointer, ledger):
    # Live phase 9b: a run escalated by the approval agent showed "No data" for why.
    seed_history(ledger, "Late Escalation Supply", [100, 120, 110])
    run(checkpointer, ledger, FakeLLM(invoice(vendor="Late Escalation Supply", total=90.0), decision="escalate"))
    kinds = {p.attributes["kind"]: p.value for p in points("warden.escalations")}
    assert kinds.get("approval_agent", 0) >= 1
    span = by_name("step pending_human")[-1]
    assert "approval_agent" in span.attributes["warden.escalation_kinds"]


class Collector(BaseHTTPRequestHandler):
    received: dict = {}

    def log_message(self, *args):
        pass

    def do_POST(self):
        Collector.received.setdefault(self.path, []).append(self.rfile.read(int(self.headers["content-length"])))
        self.send_response(200)
        self.send_header("content-type", "application/x-protobuf")
        self.send_header("content-length", "0")
        self.end_headers()


EXPORT = r"""
import uuid
from app import telemetry
assert telemetry.configure()
from opentelemetry import metrics, trace, _logs
with telemetry.run_span(str(uuid.uuid4()), "submit") as span:
    telemetry.traced_step("risk_scoring", lambda s: {"audit_trail": [
        {"step": "risk_scoring", "risk_score": 0.6, "route": "warden-routine"}]})({"run_id": "r-1"})
trace.get_tracer_provider().force_flush()
metrics.get_meter_provider().force_flush()
_logs.get_logger_provider().force_flush()
"""


def test_otlp_export_reaches_a_collector():
    from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
    from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    server = ThreadingHTTPServer(("127.0.0.1", 0), Collector)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        env = {"OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{server.server_port}", "PATH": "/usr/bin:/bin"}
        out = subprocess.run([sys.executable, "-c", EXPORT], cwd=Path(__file__).resolve().parents[1], env=env,
                             capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr[-2000:]
    finally:
        server.shutdown()
    assert set(Collector.received) == {"/v1/traces", "/v1/metrics", "/v1/logs"}
    traces = ExportTraceServiceRequest.FromString(Collector.received["/v1/traces"][0])
    resource = {a.key: a.value.string_value for a in traces.resource_spans[0].resource.attributes}
    assert resource["service.name"] == "warden-backend" and resource["service.namespace"] == "warden"
    names = {s.name for rs in traces.resource_spans for ss in rs.scope_spans for s in ss.spans}
    assert names == {"invoice run (submit)", "step risk_scoring"}
    metric_names = {m.name for raw in Collector.received["/v1/metrics"]
                    for rm in ExportMetricsServiceRequest.FromString(raw).resource_metrics
                    for sm in rm.scope_metrics for m in sm.metrics}
    assert {"warden.agent.duration", "warden.risk.score"} <= metric_names
    log = ExportLogsServiceRequest.FromString(Collector.received["/v1/logs"][0])
    body = log.resource_logs[0].scope_logs[0].log_records[0].body.string_value
    assert json.loads(body)["risk_score"] == 0.6


class Wobbly(FakeLLM):
    """A model that answers the same risk question differently each time, as a real one may."""

    def __init__(self, *args, scores, decisions, **kwargs):
        super().__init__(*args, **kwargs)
        self.scores, self.decisions, self.sent = iter(scores), iter(decisions), []

    def __call__(self, agent, route, system, user, model):
        from app.schemas import ApprovalDecision
        self.sent.append((agent, user))
        if model is RiskAssessment:
            score = next(self.scores)
            level = "low" if score < 0.34 else "medium" if score < 0.67 else "high"
            return RiskAssessment(risk_level=level, risk_score=score, signals=[], rationale="x")
        if model is ApprovalDecision:
            decision, confidence = next(self.decisions)
            return ApprovalDecision(decision=decision, confidence=confidence, reasons=["x"])
        return super().__call__(agent, route, system, user, model)


def test_consistency_repeats_the_same_question_and_measures_the_spread(checkpointer, ledger):
    from app import consistency
    from app.agents import Deps
    run_id = str(uuid.uuid4())
    graph, cfg = run(checkpointer, ledger, FakeLLM(invoice(vendor="Kestrel Print Works", total=90.0)), run_id)
    values = graph.get_state(cfg).values

    # Repeat 1: escalate at once. Repeat 2: three auto_approves. Repeat 3: auto_approve, then escalate.
    model = Wobbly(invoice(), scores=[0.6, 0.15, 0.35], decisions=[
        ("escalate", 0.7), ("auto_approve", 0.9), ("auto_approve", 0.95), ("auto_approve", 0.9),
        ("auto_approve", 0.8), ("escalate", 0.6)])
    report = consistency.measure(Deps(llm=model), values, times=3)
    assert report["risk"]["scores"] == [0.6, 0.15, 0.35] and report["risk"]["spread"] == 0.45
    assert report["risk"]["original"] == 0.1          # what the run itself got
    approval = report["approval"]
    assert approval["decisions"] == ["escalate", "auto_approve", "escalate"]
    assert approval["answers"] == [["escalate"], ["auto_approve"] * 3, ["auto_approve", "escalate"]]
    assert approval["asks"] == 6 and approval["raw_auto_approve"] == 4
    assert approval["confidences"] == [0.7, 0.9, 0.6]   # the lowest of each repeat's asks
    assert approval["agree"] is False and approval["spread"] == 0.3
    # Every repeat sent the evidence the run sent: same text each time, for each agent.
    assert len({user for agent, user in model.sent if agent == "risk-scoring"}) == 1
    assert len({user for agent, user in model.sent if agent == "approval"}) == 1
    assert "DIFFER" in consistency.describe(report) and "the model's, not the invoice's" in consistency.describe(report)

    consistency.record(report)
    spread = {p.attributes["agent"]: p.value for p in points("warden.consistency.spread")
              if p.attributes["vendor"] == "Kestrel Print Works"}
    assert spread == {"risk-scoring": 0.45, "approval": 0.3}
    assert any(line.get("step") == "consistency" for line in step_lines())


def test_consistency_refuses_a_run_that_never_reached_the_risk_agent():
    from app import consistency
    from app.agents import Deps
    with pytest.raises(ValueError, match="stopped before the risk agent"):
        consistency.measure(Deps(llm=lambda *a: None), {"run_id": "x", "status": "blocked"}, times=2)


def test_consistency_command_loads_a_saved_run_by_id(checkpointer, ledger, monkeypatch, capsys):
    from app import consistency
    from app.agents import Deps
    run_id = str(uuid.uuid4())
    run(checkpointer, ledger, FakeLLM(invoice(vendor="Saved Run Supplies", total=60.0)), run_id)
    model = Wobbly(invoice(), scores=[0.2, 0.2], decisions=[("escalate", 0.8), ("escalate", 0.8)])
    monkeypatch.setattr(consistency, "Deps", lambda: Deps(llm=model))
    assert consistency.main([run_id, "--times", "2"]) == 0
    out = capsys.readouterr().out
    assert "Saved Run Supplies" in out and "Spread 0.00" in out and "all agree" in out
    assert consistency.main([str(uuid.uuid4())]) == 2   # an unknown run is refused, not crashed on


def test_a_temperature_set_for_an_agent_reaches_the_model_and_the_trace(monkeypatch):
    from app import config
    sent = {}
    completion = SimpleNamespace(model="m", usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))
    answer = RiskAssessment(risk_level="low", risk_score=0.1, signals=[], rationale="x")

    def create_with_completion(**kwargs):
        sent.update(kwargs)
        return answer, completion
    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create_with_completion=create_with_completion)))
    monkeypatch.setattr(llm, "_client", lambda agent: fake)
    monkeypatch.setattr(config, "AGENT_TEMPERATURES", config._temperatures("risk-scoring=0, approval = 0.2"))
    llm.structured("risk-scoring", "warden-routine", "s", "u", RiskAssessment)
    assert sent["temperature"] == 0.0
    assert by_name("chat warden-routine")[-1].attributes["gen_ai.request.temperature"] == 0.0
    sent.clear()
    llm.structured("critic", "warden-routine", "s", "u", RiskAssessment)
    assert "temperature" not in sent        # other agents keep the provider's default


def test_the_report_names_the_temperature_and_explains_an_unasked_agent(monkeypatch):
    from app import config, consistency
    from app.agents import Deps
    monkeypatch.setattr(config, "AGENT_TEMPERATURES", {"risk-scoring": 0.0})
    values = {"run_id": "r", "extraction": invoice(vendor="Kestrel Print Works"), "vendor": {"known": True},
              "risk": {"risk_score": 0.6}}
    model = Wobbly(invoice(), scores=[0.6, 0.6], decisions=[("escalate", 0.8), ("escalate", 0.8)])
    text = consistency.describe(consistency.measure(Deps(llm=model), values, times=2))
    assert "Temperature: risk-scoring 0" in text
    assert "not asked (the run went to a person straight after the risk step)" in text


@pytest.mark.parametrize("answers, expected, asks", [
    pytest.param(["auto_approve"] * 3, "auto_approve", 3, id="unanimous: approved"),
    pytest.param(["auto_approve", "auto_approve", "escalate"], "escalate", 3, id="one dissent: a person decides"),
    pytest.param(["auto_approve", "reject"], "escalate", 2, id="a later reject: a person decides, not a reject"),
    pytest.param(["escalate"], "escalate", 1, id="escalate on the first ask: no more asks"),
    pytest.param(["reject"], "reject", 1, id="reject on the first ask stands"),
])
def test_an_auto_approval_must_be_unanimous(answers, expected, asks):
    from app.agents import Deps
    from app.agents.approval import approve
    from app.schemas import ApprovalDecision
    queue = iter(zip(answers, [0.9, 0.8, 0.95]))
    calls = []

    def model(agent, route, system, user, response_model):
        decision, confidence = next(queue)
        calls.append(decision)
        return ApprovalDecision(decision=decision, confidence=confidence, reasons=[f"said {decision}"])
    final, samples = approve(Deps(llm=model), invoice(), {"known": True}, {"risk_score": 0.1}, {})
    assert final.decision == expected and len(samples) == len(calls) == asks
    assert final.confidence == min(s.confidence for s in samples)
    if expected == "escalate" and answers[0] == "auto_approve":
        assert final.reasons[0].startswith("not unanimous: ")


def test_one_ask_when_unanimity_is_turned_off(monkeypatch):
    from app import config
    from app.agents import Deps
    from app.agents.approval import approve
    from app.schemas import ApprovalDecision
    monkeypatch.setattr(config, "APPROVAL_SAMPLES", 1)
    calls = []
    final, samples = approve(Deps(llm=lambda *a: calls.append(1) or ApprovalDecision(
        decision="auto_approve", confidence=0.9, reasons=[])), invoice(), {}, {}, {})
    assert final.decision == "auto_approve" and len(calls) == 1
