"""Builds infra/azure/workbook.json, the Azure counterpart of infra/grafana's dashboard
(phase 11c): the same questions, asked of Application Insights in KQL.

    python infra/azure/build_workbook.py

The OpenTelemetry metrics arrive in customMetrics with their attributes in
customDimensions. Azure Monitor sends a counter as the amount added in each
export interval (valueSum), and a histogram as its sum, count, min and max per
interval: no buckets, so these panels show averages and maxima where Grafana
shows percentiles. Requests and calls between services arrive in requests and
dependencies, under each service's name (cloud_RoleName)."""
import json
from pathlib import Path

OUT = Path(__file__).with_name("workbook.json")


def text(markdown: str) -> dict:
    return {"type": 1, "content": {"json": markdown}}


def query(title: str, kql: str, chart: str = "table", width: str = "50") -> dict:
    return {"type": 3, "customWidth": width, "content": {
        "version": "KqlItem/1.0", "title": title, "query": kql.strip(), "size": 0,
        "queryType": 0, "resourceType": "microsoft.insights/components",
        "timeContextFromParameter": "TimeRange", "visualization": chart}}


def metric(name: str) -> str:
    return f'customMetrics\n| where name == "{name}"'


ITEMS = [
    text("# Warden\nRuns, agents, models and services, from Application Insights. The same questions as the "
         "local Grafana dashboard; histograms show averages and maxima here (Azure Monitor sends no buckets)."),
    {"type": 9, "content": {"version": "KqlParameterItem/1.0", "parameters": [{
        "id": "time-range", "version": "KqlParameterItem/1.0", "name": "TimeRange", "label": "Time range",
        "type": 4, "isRequired": True, "value": {"durationMs": 604800000},
        "typeSettings": {"selectableValues": [{"durationMs": d} for d in
                                              (3600000, 86400000, 604800000, 2592000000)]}}]}},

    text("## Runs"),
    query("Runs by outcome", metric("warden.runs") + """
| extend outcome = tostring(customDimensions.outcome)
| summarize runs = sum(valueSum) by outcome
| order by runs desc""", "barchart"),
    query("Why runs went to a person", metric("warden.escalations") + """
| extend reason = tostring(customDimensions.kind)
| summarize escalations = sum(valueSum) by reason
| order by escalations desc""", "barchart"),
    query("Runs over time", metric("warden.runs") + """
| extend outcome = tostring(customDimensions.outcome)
| summarize runs = sum(valueSum) by bin(timestamp, 1h), outcome""", "timechart", "100"),

    text("## Agents and models"),
    query("Time per graph step (seconds)", metric("warden.agent.duration") + """
| extend step = tostring(customDimensions.step)
| summarize average = round(sum(valueSum) / sum(valueCount), 2), slowest = round(max(valueMax), 2),
            steps = sum(valueCount) by step
| order by average desc"""),
    query("Model tokens by agent", metric("warden.llm.tokens") + """
| extend agent = tostring(customDimensions.agent), kind = tostring(customDimensions.kind)
| summarize tokens = sum(valueSum) by agent, kind
| order by tokens desc""", "barchart"),
    query("Time per model call (seconds)", metric("warden.llm.duration") + """
| extend agent = tostring(customDimensions.agent), route = tostring(customDimensions.route)
| summarize average = round(sum(valueSum) / sum(valueCount), 2), slowest = round(max(valueMax), 2),
            calls = sum(valueCount) by agent, route
| order by average desc"""),
    query("Risk scores the risk agent gave", metric("warden.risk.score") + """
| extend route = tostring(customDimensions.route)
| summarize average = round(sum(valueSum) / sum(valueCount), 2), lowest = round(min(valueMin), 2),
            highest = round(max(valueMax), 2), scored = sum(valueCount) by route"""),
    query("Policy decisions", metric("warden.policy.decisions") + """
| extend decision = tostring(customDimensions.decision), principal = tostring(customDimensions.principal)
| summarize decisions = sum(valueSum) by decision, principal
| order by decisions desc"""),
    query("Consistency: spread of repeated scores", metric("warden.consistency.spread") + """
| extend agent = tostring(customDimensions.agent)
| summarize arg_max(timestamp, value) by agent
| project agent, spread = round(value, 2), measured = timestamp"""),

    text("## Services"),
    query("Requests each service served", """
requests
| where name !has "health"
| summarize requests = count(), failed = countif(success == false),
            average_ms = round(avg(duration), 1), p95_ms = round(percentile(duration, 95), 1) by service = cloud_RoleName
| order by requests desc"""),
    query("Calls between services", """
dependencies
| summarize calls = count(), failed = countif(success == false), average_ms = round(avg(duration), 1)
            by from = cloud_RoleName, to = target
| order by calls desc"""),
    query("Failed requests", """
requests
| where success == false
| project timestamp, service = cloud_RoleName, name, resultCode, duration, operation_Id
| order by timestamp desc
| take 50""", width="100"),
    query("The backend's step log", """
traces
| where cloud_RoleName == "warden-backend"
| project timestamp, message
| order by timestamp desc
| take 50""", width="100"),
]

WORKBOOK = {"version": "Notebook/1.0", "items": ITEMS, "isLocked": False,
            "fallbackResourceIds": ["Azure Monitor"]}

if __name__ == "__main__":
    OUT.write_text(json.dumps(WORKBOOK, indent=2) + "\n")
    print(f"wrote {OUT}")
