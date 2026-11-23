"""Reproduce observation features locally; offline mutations are explicitly recorded."""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from typing import Any

from .masking import mask_any
from ..simulation import run_scenario
from .annotation import create_queue, enqueue_low_scores
from .capabilities import sdk_call, capabilities
from .client import SafeClient, make_client
from .datasets import scenario_examples, push_dataset, from_runs
from .distributed import trace_headers, parent_scope
from .evals import evaluate_scenarios, summarize, sdk_evaluate
from .feedback import feedback, user_feedback_token
from .offline import OfflineClient, capture_server
from .online import setup
from .otel import configuration
from .pairwise import compare
from .prompts import PromptHub, prompt_diff
from .report import session_report, runs_from_capture
from .safe import safe_call
from .tracing import Observation, tracing_scope, span

FEATURES = [
    (1, "trace", "Graph tracing", "Project / Runs"), (2, "trace", "Child spans", "Run tree / filter:*"),
    (3, "mask", "Wire masking", "Run / Inputs and outputs"), (4, "threads", "Threads", "Project / Threads"),
    (5, "distributed", "Distributed tracing", "Run tree / distributed-worker"),
    (6, "query", "Metadata queries", "Project / Runs / Filters"),
    (7, "feedback", "Code feedback", "Run / Feedback"), (8, "feedback", "User feedback", "Run / user_score"),
    (9, "dataset", "Scenario dataset", "Datasets / orchestra-scenarios"),
    (10, "dataset", "Production regression dataset", "Datasets / orchestra-regressions"),
    (11, "eval", "Offline evaluation", "Experiments / orchestra-eval"),
    (12, "eval", "Safety evaluators", "Experiments / payment_safety"),
    (13, "pairwise", "Pairwise comparison", "Experiments / Compare"),
    (14, "pytest", "Pytest integration", "Experiments / pytest"),
    (15, "prompts", "Prompt Hub", "Prompts / orchestra-*"),
    (16, "annotate", "Annotation queue", "Annotation queues / orchestra-review"),
    (17, "online", "Online evaluators", "Project / Rules"),
    (18, "monitor", "Monitoring and alerts", "Project / Monitoring"),
    (19, "monitor", "Token cost", "Project / Monitoring / Costs"),
    (20, "share", "Explicit sharing", "Run / Share"), (21, "otel", "OpenTelemetry", "Project / Runs / OTel"),
    (22, "studio", "Studio", "Local Studio / Graph"),
    (23, "insights", "Insights / Engine / Context Hub", "Workspace / Insights"),
    (24, "report", "Session report", "Local report.html"),
]


def trace_demo(mode: str, endpoint: str) -> dict:
    """Send actual graph SDK traces to the loopback receiver, including approval/resume."""
    from langchain_core.tracers.langchain import wait_for_all_tracers

    client = make_client(mode, api_url=endpoint, api_key="local-test", auto_batch_tracing=False)
    observer = Observation(org="travel_payment", domains=["travel", "payment"], mode=mode,
                           client=client, enabled=True, metadata={"scenario_id": "jeju_trip_payment",
                                                                  "sandbox": "fake"})
    headers = {}
    with tracing_scope(mode, client):
        with span("orchestra:travel_payment:hierarchical", client=client,
                  inputs={"text": "900101-1234567 private@example.com 1234-5678-1234-5678 cvc=123"}):
            result = run_scenario("jeju_trip_payment", security_mode=mode, observation=observer)
            headers = trace_headers(mode)
            with parent_scope(headers, mode=mode, client=client):
                with span("distributed-worker", client=client, inputs={"work": "offline correlation check"}):
                    pass
    safe_call(wait_for_all_tracers)
    return {"answer": mask_any(result.final), "headers": headers,
            "config": mask_any({k: v for k, v in observer.config_hook({}, "wire-thread").items() if k != "callbacks"})}


def _demo_runs() -> list:
    start = datetime.now(timezone.utc)
    return [SimpleNamespace(id=uuid4(), name=name, start_time=start, end_time=start + timedelta(seconds=seconds),
                            total_tokens=tokens, error=None, inputs={"task": name}, outputs={"answer": "done"},
                            feedback_stats={"quality": {"avg": score}}, parent_run_id=None)
            for name, seconds, tokens, score in (("team:planning_lead", 1, 240, 1), ("agent:hotel_agent", 2, 120, 0))]


def main(argv: list[str] | None = None) -> int:
    """Run one feature or all 24, writing only sanitized local artifacts offline."""
    commands = sorted({feature[1] for feature in FEATURES} | {"all"})
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=commands)
    parser.add_argument("action", nargs="?")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("out/showcase"))
    parser.add_argument("--security-mode", "--mode", default="masked")
    parser.add_argument("--org", default="travel_payment")
    parser.add_argument("--project", default=os.environ.get("LANGSMITH_PROJECT", "orchestra-dev"))
    parser.add_argument("--pattern", default="hierarchical")
    parser.add_argument("--deep", action="store_true")
    parser.add_argument("--user", action="store_true")
    parser.add_argument("--cost", action="store_true")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--tag", default="org:travel_payment")
    parser.add_argument("--evaluators", default="all")
    parser.add_argument("--a", default="hierarchical")
    parser.add_argument("--b", default="supervisor")
    args = parser.parse_args(argv)
    if args.org != "travel_payment" or args.pattern != "hierarchical":
        parser.error("this scripted showcase currently supports travel_payment / hierarchical")
    args.output.mkdir(parents=True, exist_ok=True)
    raw = OfflineClient() if args.offline else None
    client = SafeClient(raw, args.security_mode) if args.offline else make_client(args.security_mode)
    selected = [row for row in FEATURES if args.command in {row[1], "all"}]
    results: dict[str, Any] = {}
    run_id = str(uuid4())
    runs = _demo_runs()
    if any(row[1] in {"trace", "mask", "threads", "distributed", "report", "monitor"} for row in selected) and not args.demo:
        if not args.offline:
            parser.error("trace demonstrations require --offline to avoid implicit remote writes")
        with capture_server() as (endpoint, requests):
            results["trace"] = trace_demo(args.security_mode, endpoint)
        results["wire"] = requests
        runs = runs_from_capture(requests) if requests else []
        if args.security_mode == "closed":
            assert not requests, "closed mode sent HTTP traffic"
        else:
            wire = json.dumps(requests)
            assert requests and "REDACTED" in wire, "masked SDK trace was not received"
            for secret in ("900101-1234567", "private@example.com", "1234-5678-1234-5678", "cvc=123"):
                assert secret not in wire, "raw PII/PCI on wire"
    for _, command, _, _ in selected:
        if command in results:
            continue
        if command == "query":
            results[command] = sdk_call(client, "list_runs", project_name=args.project, filter=f'has(tags,{json.dumps(args.tag)})')
        elif command == "feedback":
            if not args.offline and not args.action:
                parser.error("feedback requires an existing run ID outside offline mode")
            run_id = args.action or run_id
            results[command] = [feedback(client, run_id, "demo_score", 1, "Showcase demonstration, not a safety certification"),
                                user_feedback_token(client, run_id)]
        elif command == "dataset":
            examples = scenario_examples()
            if args.action == "from-runs" and not args.offline:
                from .monitoring import fetch_runs

                runs = fetch_runs(args.project, client=client)
            regressions = from_runs(runs, score_key="quality")
            results[command] = {"examples": examples, "upload": push_dataset(client, examples) if args.action != "from-runs" else None,
                                "regressions": regressions, "regression_upload": push_dataset(
                                    client, regressions, name="orchestra-regressions") if regressions else None}
        elif command == "eval":
            rows = evaluate_scenarios()
            # Exercise the actual installed SDK locally, rather than relabeling custom scores as an SDK experiment.
            sdk_result = sdk_evaluate(lambda inputs: {"answer": inputs["value"]},
                                      [{"inputs": {"value": "local"}, "outputs": {"answer": "local"}}],
                                      [lambda outputs, reference_outputs: {"key": "exact", "score": float(outputs == reference_outputs)}])
            results[command] = {"rows": rows, "summary": summarize(rows), "sdk_executed": sdk_result is not None}
            assert sdk_result is not None, "offline SDK evaluation failed"
        elif command == "pairwise":
            results[command] = compare({"pattern": args.a}, {"pattern": args.b})
        elif command == "prompts":
            from ..catalog import PluginCatalog
            from ..org import load_org
            from ..registry import resolve, build_packs

            hub = PromptHub(client)
            results[command] = {}
            if args.action in {None, "push"}:
                org = load_org("orgs/travel_payment.yaml")
                agents = resolve(org, build_packs(org.domains)).org.agents
                results[command]["push"] = {**hub.push_agents(agents), **hub.push_agents(PluginCatalog.load().agents)}
            if args.action in {None, "pull"}:
                results[command]["pull"] = str(hub.pull("orchestra-demo", "Local fallback"))
            if args.action in {None, "diff"}:
                results[command]["diff"] = prompt_diff("before", "after")
        elif command == "annotate":
            if not args.offline and not args.action:
                parser.error("annotation routing requires an existing run ID outside offline mode")
            queue = create_queue(client)
            results[command] = {"queue": queue, "enqueue": enqueue_low_scores(client, str(getattr(queue, "id", "")),
                                                                          {args.action or run_id: 0}) if queue else None}
        elif command == "online":
            results[command] = setup(client, "orchestra-sim")
        elif command == "monitor":
            results[command] = {"html": "report.html", "price_source": "ORCHESTRA_PRICE_TABLE", "unpriced_cost": None}
        elif command == "share":
            results[command] = sdk_call(client, "share_run", args.action) if args.action else {"status": "disabled; explicit run ID required"}
        elif command == "otel":
            results[command] = configuration(args.security_mode)
        elif command in {"pytest", "studio", "insights"}:
            results[command] = {"status": "see UI/integration guide", "guide": "docs/langsmith-ui-setup.md"}
        else:
            results[command] = {"status": "demonstrated by trace", "artifact": "wire"}
    (args.output / "report.html").write_text(session_report(runs), encoding="utf-8")
    manifest = {"offline": args.offline, "features": [{"id": row[0], "command": row[1], "name": row[2],
                                                      "where": row[3]} for row in selected],
                "capabilities": capabilities(client), "results": results,
                "recorded_sdk_calls": raw.calls if raw else [],
                "limitations": "offline mutations are local recordings; LLM judge and hosted UI are not verified"}
    (args.output / "manifest.json").write_text(json.dumps(
        mask_any(manifest), ensure_ascii=False, indent=2,
        default=lambda value: {"type": type(value).__name__, "status": "SDK result returned; private details omitted"},
    ), encoding="utf-8")
    for number, command, name, where in selected:
        print(f"{number:02d} {name} ({command}) — LangSmith에서 확인할 곳: {where}")
    return 0


if __name__ == "__main__":
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
