"""Writes dashboards/warden.json. The dashboard is generated so its queries live in one
readable place; run `python infra/grafana/build_dashboard.py` after changing them."""
import json
from pathlib import Path

PROM = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}
TEMPO = {"type": "tempo", "uid": "tempo"}
STEPS = '{service_name="warden-backend"} | json | __error__=""'

panels, y = [], 0


def row(title):
    global y
    panels.append({"type": "row", "title": title, "collapsed": False, "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
                   "id": len(panels) + 1, "panels": []})
    y += 1


def panel(kind, title, description, targets, w=12, h=8, x=0, **extra):
    panels.append({"type": kind, "title": title, "description": description, "id": len(panels) + 1,
                   "gridPos": {"h": h, "w": w, "x": x, "y": y}, "targets": targets, **extra})


def prom(expr, legend="", instant=False, fmt="time_series"):
    return {"datasource": PROM, "expr": expr, "legendFormat": legend, "refId": chr(65 + len(_refs)),
            "instant": instant, "range": not instant, "format": fmt}


_refs = []


def loki(expr, instant=True, legend=""):
    return {"datasource": LOKI, "expr": expr, "queryType": "instant" if instant else "range",
            "legendFormat": legend, "refId": "A"}


row("Runs")
panel("bargauge", "Run segments by outcome", "How each stretch of a run ended, over the dashboard's time range: "
      "settled, waiting for a person (pending_human), rejected, blocked, failed or stopped by the kill switch.",
      [prom('sum by (outcome) (increase(warden_runs_total[$__range]))', "{{outcome}}", instant=True)], w=8,
      datasource=PROM, options={"orientation": "horizontal", "displayMode": "basic"},
      fieldConfig={"defaults": {"unit": "short", "decimals": 0}, "overrides": []})
panel("bargauge", "Why invoices went to a person", "Escalation reasons from the risk step, grouped by kind.",
      [prom('sum by (kind) (increase(warden_escalations_total[$__range]))', "{{kind}}", instant=True)], w=8, x=8,
      datasource=PROM, options={"orientation": "horizontal", "displayMode": "basic"},
      fieldConfig={"defaults": {"unit": "short", "decimals": 0}, "overrides": []})
panel("bargauge", "Policy decisions", "Cedar decisions, for agents and for people.",
      [prom('sum by (decision, principal) (increase(warden_policy_decisions_total[$__range]))',
            "{{principal}} {{decision}}", instant=True)], w=8, x=16, datasource=PROM,
      options={"orientation": "horizontal", "displayMode": "basic"},
      fieldConfig={"defaults": {"unit": "short", "decimals": 0}, "overrides": []})
y += 8

row("Where the time goes")
panel("timeseries", "Step latency, 95th percentile", "Time per graph step. A slow model shows here first.",
      [prom('histogram_quantile(0.95, sum by (le, step) (rate(warden_agent_duration_seconds_bucket'
            '{outcome="ok"}[$__rate_interval])))', "{{step}}")], datasource=PROM,
      fieldConfig={"defaults": {"unit": "s"}, "overrides": []})
panel("timeseries", "Model call latency by agent, 95th percentile", "Time per call to the model gateway.",
      [prom('histogram_quantile(0.95, sum by (le, agent) (rate(warden_llm_duration_seconds_bucket'
            '[$__rate_interval])))', "{{agent}}")], x=12, datasource=PROM,
      fieldConfig={"defaults": {"unit": "s"}, "overrides": []})
y += 8
panel("timeseries", "Service latency, 95th percentile", "Every instrumented service's HTTP requests: backend, "
      "policy, ledger, control plane, data quality.",
      [prom('histogram_quantile(0.95, sum by (le, service_name) (rate(http_server_request_duration_seconds_bucket'
            '[$__rate_interval])))', "{{service_name}}")], datasource=PROM,
      fieldConfig={"defaults": {"unit": "s"}, "overrides": []})
panel("bargauge", "Model tokens by agent", "Input and output tokens per agent over the time range. Budgets are "
      "on the console's Governance page.",
      [prom('sum by (agent, kind) (increase(warden_llm_tokens_total[$__range]))', "{{agent}} {{kind}}",
            instant=True)], x=12, datasource=PROM, options={"orientation": "horizontal", "displayMode": "basic"},
      fieldConfig={"defaults": {"unit": "short", "decimals": 0}, "overrides": []})
y += 8

row("Are the agents consistent?")
panel("table", "Risk scores per vendor", "Every risk score each vendor got in the time range, from the step "
      "logs. A wide spread on similar invoices is worth a consistency check (see the README).",
      [dict(loki(f'max_over_time({STEPS} | step="risk_scoring" | unwrap risk_score [$__range]) by (vendor)'),
            refId="A"),
       dict(loki(f'min_over_time({STEPS} | step="risk_scoring" | unwrap risk_score [$__range]) by (vendor)'),
            refId="B"),
       dict(loki(f'sum by (vendor) (count_over_time({STEPS} | step="risk_scoring" [$__range]))'), refId="C")],
      w=14, h=9, datasource=LOKI,
      transformations=[{"id": "merge", "options": {}},
                       {"id": "organize", "options": {"excludeByName": {"Time": True},
                                                      "renameByName": {"Value #A": "Highest", "Value #B": "Lowest",
                                                                       "Value #C": "Runs scored",
                                                                       "vendor": "Vendor"}}}],
      fieldConfig={"defaults": {"decimals": 2}, "overrides": []})
panel("table", "Consistency checks", "The spread (highest minus lowest) when the same invoice was scored again "
      "and again with python -m app.consistency. Above 0.2 for risk means the model, not the invoice, is moving.",
      [prom('max by (vendor, agent, field) (last_over_time(warden_consistency_spread[$__range]))', instant=True,
            fmt="table")], w=10, h=9, x=14, datasource=PROM,
      transformations=[{"id": "organize", "options": {"excludeByName": {"Time": True},
                                                      "renameByName": {"Value": "Spread", "vendor": "Vendor",
                                                                       "agent": "Agent", "field": "Measured"}}}],
      fieldConfig={"defaults": {"decimals": 2, "thresholds": {"mode": "absolute", "steps": [
          {"color": "green", "value": None}, {"color": "orange", "value": 0.1}, {"color": "red", "value": 0.2}]},
          "custom": {"cellOptions": {"type": "color-text"}}}, "overrides": []})
y += 9
panel("barchart", "Risk score distribution", "How many risk scores fell in each band over the time range.",
      [prom('sum by (le) (increase(warden_risk_score_bucket[$__range]))', "{{le}}", instant=True, fmt="table")],
      w=24, h=7, datasource=PROM,
      transformations=[{"id": "sortBy", "options": {"sort": [{"field": "le"}]}}],
      options={"xField": "le"}, fieldConfig={"defaults": {"decimals": 0}, "overrides": []})
y += 7

row("Recent runs")
panel("table", "Run traces", "One trace per stretch of a run. Open one to see every step, model call and service "
      "call in order, with timings.",
      [{"datasource": TEMPO, "queryType": "traceql", "refId": "A", "limit": 30, "tableType": "traces",
        "query": '{resource.service.name="warden-backend" && name=~"invoice run.*"}'}],
      w=24, h=9, datasource=TEMPO)
y += 9
panel("logs", "Step log", "Each step's trail entry as it happened, the same facts the audit log holds.",
      [loki(STEPS.replace(' | json | __error__=""', ''), instant=False)], w=24, h=10, datasource=LOKI,
      options={"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending"})

dashboard = {"uid": "warden", "title": "Warden", "tags": ["warden"], "timezone": "browser", "editable": True,
             "schemaVersion": 39, "time": {"from": "now-24h", "to": "now"}, "refresh": "30s", "panels": panels,
             "templating": {"list": []}, "annotations": {"list": []}}
out = Path(__file__).resolve().parent / "dashboards" / "warden.json"
out.write_text(json.dumps(dashboard, indent=2) + "\n")
print(f"wrote {out} with {sum(p['type'] != 'row' for p in panels)} panels")
