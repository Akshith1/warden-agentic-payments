"""Phase 9b: what happened, how long it took, and how consistent the agents are,
over OpenTelemetry.

* Traces: one per run segment (submit to the human pause; resume to the end),
  with a span per graph step and per model call, joined to the policy, ledger
  and control services' spans through the HTTP calls between them.
* Metrics: runs by outcome, step latency, model tokens, risk scores,
  escalations by kind, policy decisions, and the spread of repeated scoring.
* Step logs: each trail entry the agents already write, as one JSON line, so
  Grafana can line up, say, every risk score a vendor ever got.

What leaves the backend is what the audit log already holds: ids, vendor
names, amounts, scores and decisions. Never invoice text, never prompts, never
a person's note. Personal values don't reach the trail in the first place
(phase 7); the note field is dropped here.

Off unless OTEL_EXPORTER_OTLP_ENDPOINT is set; the API's no-op providers make
every call below free in tests and local runs."""
import json
import logging
import os
import time
from contextlib import contextmanager
from typing import Callable

from langgraph.errors import GraphInterrupt
from opentelemetry import metrics, trace
from opentelemetry.trace import Link, Status, StatusCode

SERVICE = "warden-backend"
tracer = trace.get_tracer("warden.backend")
meter = metrics.get_meter("warden.backend")
steps_log = logging.getLogger("warden.steps")
steps_log.propagate = False   # step lines go to telemetry only, not the container log
steps_log.setLevel(logging.INFO)

RUNS = meter.create_counter("warden.runs", unit="{run}",
                            description="Run segments by how they ended")
STEP_DURATION = meter.create_histogram("warden.agent.duration", unit="s", description="Time per graph step",
                                       explicit_bucket_boundaries_advisory=[0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20,
                                                                            30, 60, 120])
LLM_TOKENS = meter.create_counter("warden.llm.tokens", unit="{token}", description="Model tokens by agent")
LLM_DURATION = meter.create_histogram("warden.llm.duration", unit="s", description="Time per model call",
                                      explicit_bucket_boundaries_advisory=[0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120])
RISK_SCORE = meter.create_histogram("warden.risk.score", description="Risk scores the risk agent gave",
                                    explicit_bucket_boundaries_advisory=[i / 10 for i in range(1, 11)])
ESCALATIONS = meter.create_counter("warden.escalations", unit="{escalation}",
                                   description="Reasons a run was sent to a person")
POLICY = meter.create_counter("warden.policy.decisions", unit="{decision}", description="Cedar decisions")
SPREAD = meter.create_gauge("warden.consistency.spread",
                            description="Highest minus lowest value over repeated scoring of one invoice")

# Escalation reasons are sentences; group them into a few kinds for a label.
# First match wins, so the more specific phrase comes first.
REASON_KINDS = [("first-seen", "first_seen"), ("limited history", "limited_history"),
                ("high risk", "high_risk"), ("remit-to account", "remit_changed"), ("currency", "currency"),
                ("amount above", "amount_over_threshold"), ("decision confidence", "low_confidence"),
                ("not unanimous", "approval_not_unanimous"), ("approval agent escalated", "approval_agent"),
                ("critic disagreed", "critic"), ("policy:", "policy")]


def reason_kind(reason: str) -> str:
    return next((kind for text, kind in REASON_KINDS if text in reason.lower()), "other")


def configure(span_exporter=None, metric_readers=None, log_exporter=None) -> bool:
    """Sets up exporters: the ones passed in (tests), else OTLP over HTTP when
    OTEL_EXPORTER_OTLP_ENDPOINT is set (locally, Grafana), else Azure Monitor when
    APPLICATIONINSIGHTS_CONNECTION_STRING is set (phase 11c, on Azure). Returns whether
    telemetry is on."""
    from opentelemetry._logs import set_logger_provider
    from opentelemetry.instrumentation.logging.handler import LoggingHandler
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, SimpleLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

    otlp = bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"))
    azure = bool(os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING"))
    if not (otlp or azure or span_exporter or metric_readers or log_exporter):
        return False
    resource = Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", SERVICE),
                                "service.namespace": "warden"})
    if otlp and not (span_exporter or metric_readers or log_exporter):
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        spans, logs = BatchSpanProcessor(OTLPSpanExporter()), BatchLogRecordProcessor(OTLPLogExporter())
        readers = [PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=10_000)]
    elif azure and not (span_exporter or metric_readers or log_exporter):
        from azure.monitor.opentelemetry.exporter import (AzureMonitorLogExporter, AzureMonitorMetricExporter,
                                                          AzureMonitorTraceExporter)
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        spans = BatchSpanProcessor(AzureMonitorTraceExporter())
        logs = BatchLogRecordProcessor(AzureMonitorLogExporter())
        readers = [PeriodicExportingMetricReader(AzureMonitorMetricExporter(), export_interval_millis=60_000)]
    else:
        spans = SimpleSpanProcessor(span_exporter) if span_exporter else None
        logs = SimpleLogRecordProcessor(log_exporter) if log_exporter else None
        readers = metric_readers or []
    provider = TracerProvider(resource=resource)
    if spans:
        provider.add_span_processor(spans)
    trace.set_tracer_provider(provider)
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=readers))
    logger_provider = LoggerProvider(resource=resource)
    if logs:
        logger_provider.add_log_record_processor(logs)
    set_logger_provider(logger_provider)
    steps_log.addHandler(LoggingHandler(level=logging.INFO, logger_provider=logger_provider))
    return True


def instrument(app) -> None:
    """Spans for every request the backend serves and every HTTP call it makes (policy, ledger,
    control, PII, OCR, the model gateway), with trace context passed along."""
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    FastAPIInstrumentor.instrument_app(app, excluded_urls="health", exclude_spans=["receive", "send"])
    HTTPXClientInstrumentor().instrument()


def current_link() -> list[Link]:
    """A link from a run's own trace back to the request that started it."""
    ctx = trace.get_current_span().get_span_context()
    return [Link(ctx)] if ctx.is_valid else []


@contextmanager
def run_span(run_id: str, kind: str, links: list[Link] | None = None):
    with tracer.start_as_current_span(f"invoice run ({kind})", links=links or [],
                                      attributes={"warden.run_id": run_id, "warden.segment": kind}) as span:
        yield span


def run_ended(span, values: dict) -> None:
    status = values.get("status", "unknown")
    span.set_attribute("warden.status", status)
    ext = values.get("extraction") or {}
    if ext.get("vendor_name"):
        span.set_attribute("warden.vendor", ext["vendor_name"])
    RUNS.add(1, {"outcome": status})


def run_failed(span, outcome: str, exc: Exception) -> None:
    span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
    span.set_attribute("warden.status", outcome)
    RUNS.add(1, {"outcome": outcome})


# What to lift from each step's trail entry onto its span and metrics.
NUMERIC = ("risk_score", "confidence", "amount", "total_amount", "hold_seconds")
TEXT = ("risk_level", "decision", "route", "principal", "outcome", "verdict", "agrees", "prompt_version",
        "vendor_known")


def traced_step(name: str, fn: Callable) -> Callable:
    """Wraps one graph node: a span, its duration, and what its trail entries say."""
    # Only `state`: LangGraph reads a node's signature to decide what else to pass, and every
    # Warden node takes the state alone (its dependencies are bound with functools.partial).
    def step(state):
        start = time.monotonic()
        # LangGraph pauses for a person by raising; this span marks real failures itself.
        with tracer.start_as_current_span(f"step {name}", record_exception=False, set_status_on_exception=False,
                                          attributes={"warden.step": name,
                                                      "warden.run_id": str(state.get("run_id", ""))}) as span:
            outcome = "ok"
            try:
                result = fn(state)
            except GraphInterrupt:
                outcome = "paused"   # waiting for a person: not an error
                span.set_attribute("warden.paused", True)
                raise
            except Exception as exc:
                outcome = "error"
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                raise
            finally:
                STEP_DURATION.record(time.monotonic() - start, {"step": name, "outcome": outcome})
            if isinstance(result, dict):
                _describe(span, name, state, result)
            return result
    step.__name__ = getattr(fn, "__name__", name)
    return step


def _describe(span, name: str, state: dict, result: dict) -> None:
    vendor = ((result.get("extraction") or state.get("extraction") or {}).get("vendor_name"))
    for entry in result.get("audit_trail") or []:
        details = {k: v for k, v in entry.items() if k != "note"}   # a person's free text stays out
        for key in NUMERIC:
            if isinstance(details.get(key), (int, float)) and not isinstance(details.get(key), bool):
                span.set_attribute(f"warden.{key}", float(details[key]))
        for key in TEXT:
            if details.get(key) is not None:
                span.set_attribute(f"warden.{key}", str(details[key]))
        if details.get("step") == "risk_scoring" and isinstance(details.get("risk_score"), (int, float)):
            RISK_SCORE.record(float(details["risk_score"]), {"route": str(details.get("route", ""))})
        if details.get("step") == "policy" and details.get("decision"):
            principal = str(details.get("principal", "")).split(":", 1)[0] or "unknown"
            POLICY.add(1, {"decision": str(details["decision"]), "principal": principal})
        steps_log.info(json.dumps({"run_id": state.get("run_id"), "vendor": vendor, **details}, default=str))
    # Counted when a run starts waiting for a person: by then the list holds every reason, from
    # whichever step raised it (risk, approval, critic or policy). The phase 9b live run counted
    # only the risk step's and showed "No data" for a run the approval step escalated.
    reasons = state.get("escalation_reasons") if name == "pending_human" else None
    if reasons:
        for reason in reasons:
            ESCALATIONS.add(1, {"kind": reason_kind(reason)})
        span.set_attribute("warden.escalations", len(reasons))
        span.set_attribute("warden.escalation_kinds", sorted({reason_kind(r) for r in reasons}))


@contextmanager
def model_call(agent: str, route: str, temperature: float | None = None):
    """A span and metrics for one model call, named and attributed per the GenAI conventions."""
    start = time.monotonic()
    attributes = {"gen_ai.operation.name": "chat", "gen_ai.request.model": route, "warden.agent": agent}
    if temperature is not None:
        attributes["gen_ai.request.temperature"] = temperature
    with tracer.start_as_current_span(f"chat {route}", attributes=attributes) as span:
        record = {}
        try:
            yield record
        except Exception as exc:
            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            raise
        finally:
            LLM_DURATION.record(time.monotonic() - start, {"agent": agent, "route": route})
        if record.get("model"):
            span.set_attribute("gen_ai.response.model", record["model"])
        for kind in ("input", "output"):
            tokens = record.get(f"{kind}_tokens")
            if tokens:
                span.set_attribute(f"gen_ai.usage.{kind}_tokens", tokens)
                LLM_TOKENS.add(tokens, {"agent": agent, "route": route, "kind": kind})
