"""Sanitized session HTML and model-price estimates from explicit settings."""
import html
import json
import os
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from .masking import mask_any
from .monitoring import summarize_runs, render_report


def runs_from_capture(requests: list[dict]) -> list[Any]:
    """Merge real loopback create/update records into monitoring-compatible run objects."""
    records: dict[str, dict] = {}
    for request in requests:
        if not request["body"]:
            continue
        body = json.loads(request["body"])
        run_id = body.get("id") or request["path"].rsplit("/", 1)[-1]
        records.setdefault(run_id, {}).update(body)

    def usage(value: Any) -> tuple[int, int]:
        if isinstance(value, dict):
            details = value.get("usage_metadata")
            if isinstance(details, dict):
                return int(details.get("input_tokens", 0)), int(details.get("output_tokens", 0))
            parts = [usage(item) for item in value.values()]
            return sum(part[0] for part in parts), sum(part[1] for part in parts)
        if isinstance(value, list):
            parts = [usage(item) for item in value]
            return sum(part[0] for part in parts), sum(part[1] for part in parts)
        return 0, 0

    runs = []
    for identifier, record in records.items():
        input_tokens, output_tokens = usage(record.get("outputs", {}))
        model = (record.get("extra", {}).get("metadata", {}) or {}).get("ls_model_name", "")
        times = {key: datetime.fromisoformat(record[key].replace("Z", "+00:00")) if record.get(key) else None
                 for key in ("start_time", "end_time")}
        runs.append(SimpleNamespace(id=identifier, name=record.get("name", "unnamed"), **times,
                                    total_tokens=input_tokens + output_tokens, input_tokens=input_tokens,
                                    output_tokens=output_tokens, model=model, error=record.get("error"),
                                    inputs=record.get("inputs", {}), outputs=record.get("outputs", {}),
                                    parent_run_id=record.get("parent_run_id"), feedback_stats={}))
    return runs


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Estimate only when the environment supplies prices for this exact model ID."""
    prices = json.loads(os.environ.get("ORCHESTRA_PRICE_TABLE") or "{}")
    if model not in prices:
        return None
    return (input_tokens * prices[model]["in"] + output_tokens * prices[model]["out"]) / 1_000_000


def session_report(runs: list[Any]) -> str:
    """Render node/token/latency statistics and the largest sanitized prompt inputs."""
    statistics = render_report(summarize_runs(runs))
    expensive = sorted(runs, key=lambda run: getattr(run, "total_tokens", 0) or 0, reverse=True)[:10]
    prompts = [{"name": run.name, "tokens": getattr(run, "total_tokens", 0),
                "inputs": getattr(run, "inputs", {}), "parent_run_id": str(getattr(run, "parent_run_id", ""))}
               for run in expensive]
    costs = [{"name": run.name, "model": getattr(run, "model", ""),
              "estimated_cost": estimate_cost(getattr(run, "model", ""), getattr(run, "input_tokens", 0),
                                               getattr(run, "output_tokens", 0))}
             for run in runs]
    return ("<!doctype html><html lang='en'><meta charset='utf-8'><title>Orchestra session</title>"
            "<h1>Session report</h1><h2>Nodes and team tokens</h2><pre>" + html.escape(str(mask_any(statistics)))
            + "</pre><h2>Top token prompts and subagents</h2><pre>"
            + html.escape(json.dumps(mask_any(prompts), ensure_ascii=False, indent=2))
            + "</pre><h2>Per-node estimated cost (null means unpriced)</h2><pre>"
            + html.escape(json.dumps(mask_any(costs), ensure_ascii=False, indent=2)) + "</pre></html>")
