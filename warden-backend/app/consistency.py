"""How consistent are the agents? Asks the risk and approval agents the same
question several times, with exactly the evidence a past run sent them, and
reports how much the answers move.

    docker compose exec warden-backend python -m app.consistency RUN_ID [--times 5]

Read-only: nothing is paid, no run changes, nothing is written to the ledger.
Each question is a real model call through the agent's own gateway key, so it
counts against that agent's daily budget (about 2,000 tokens per question).

Why: two near-identical invoices from one vendor scored 0.60 and 0.15 in the
phase 7 live run. Whether that is the evidence or the model's own variation is
the difference between a finding about the prompt and a finding about the
invoices. The answer is also sent to Grafana as warden.consistency.spread."""
import argparse
import json
import statistics
import sys

from . import config, telemetry
from .agents import Deps
from .agents.approval import decide
from .agents.risk_scoring import assess


def measure(deps: Deps, values: dict, times: int) -> dict:
    ext, vendor = values.get("extraction"), values.get("vendor")
    if not ext or not vendor:
        raise ValueError("this run stopped before the risk agent scored it, so there is nothing to repeat")
    mapping = values.get("pii_mapping") or {}
    risks = [assess(deps, ext, vendor, mapping)[1] for _ in range(times)]
    # Approval is asked with the run's own risk assessment, so only approval's variation shows.
    original_risk = values.get("risk") or risks[0].model_dump()
    decisions = [decide(deps, ext, vendor, original_risk, mapping) for _ in range(times)]

    scores = [r.risk_score for r in risks]
    confidences = [d.confidence for d in decisions]
    return {
        "run_id": values.get("run_id"),
        "vendor": ext.get("vendor_name"),
        "times": times,
        "risk": {"original": (values.get("risk") or {}).get("risk_score"), "scores": scores,
                 "levels": [r.risk_level for r in risks], "spread": round(max(scores) - min(scores), 4),
                 "stdev": round(statistics.pstdev(scores), 4), "mean": round(statistics.fmean(scores), 4)},
        "approval": {"original": (values.get("approval") or {}).get("decision"),
                     "decisions": [d.decision for d in decisions], "confidences": confidences,
                     "spread": round(max(confidences) - min(confidences), 4),
                     "agree": len({d.decision for d in decisions}) == 1},
    }


def record(report: dict) -> None:
    attributes = {"vendor": str(report["vendor"])}
    telemetry.SPREAD.set(report["risk"]["spread"], attributes | {"agent": "risk-scoring", "field": "risk_score"})
    telemetry.SPREAD.set(report["approval"]["spread"], attributes | {"agent": "approval", "field": "confidence"})
    telemetry.steps_log.info(json.dumps({"step": "consistency", **report}, default=str))


def describe(report: dict) -> str:
    risk, approval = report["risk"], report["approval"]
    lines = [
        f"{report['vendor']}, run {report['run_id']}: asked each agent {report['times']} times with the same evidence.",
        f"Risk agent: scores {', '.join(f'{s:.2f}' for s in risk['scores'])} "
        f"(levels {', '.join(risk['levels'])}). The run itself got {risk['original']}.",
        f"  Spread {risk['spread']:.2f}, standard deviation {risk['stdev']:.2f}, mean {risk['mean']:.2f}.",
        f"Approval agent: {', '.join(approval['decisions'])}; confidence "
        f"{', '.join(f'{c:.2f}' for c in approval['confidences'])}. The run itself: {approval['original']}.",
        "  The decisions " + ("all agree." if approval["agree"] else "DIFFER between identical questions."),
    ]
    level_changes = len(set(risk["levels"])) > 1
    if level_changes or not approval["agree"]:
        lines.append("Same evidence, different answers: this variation is the model's, not the invoice's.")
    return "\n".join(lines)


def load(run_id: str) -> dict:
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    from .graph import build_graph
    with ConnectionPool(config.backend_dsn(), min_size=1, max_size=1, kwargs={"autocommit": True, "prepare_threshold": 0,
                                                                  "row_factory": dict_row}) as pool:
        graph = build_graph(Deps(), PostgresSaver(pool))
        values = graph.get_state({"configurable": {"thread_id": run_id}}).values
    if not values:
        raise ValueError(f"no run {run_id}")
    return values


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.consistency", description=__doc__.split("\n\n")[0])
    parser.add_argument("run_id", help="the run to repeat: the id at the end of its page's address")
    parser.add_argument("--times", type=int, default=5, choices=range(2, 21), metavar="2-20")
    args = parser.parse_args(argv)
    telemetry.configure()
    try:
        report = measure(Deps(), load(args.run_id), args.times)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    record(report)
    print(describe(report))
    from opentelemetry import _logs, metrics
    for provider in (metrics.get_meter_provider(), _logs.get_logger_provider()):
        getattr(provider, "force_flush", lambda: None)()   # telemetry may be off
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
