"""Scenario and production-run conversion with explicit dataset upload."""
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .masking import mask_any
from ..org import REPO_ROOT
from .capabilities import sdk_call


def scenario_examples(root: Path | None = None) -> list[dict]:
    """Convert scenario YAML into inputs/reference outputs with no SDK or network."""
    examples = []
    for path in sorted((root or REPO_ROOT / "scenarios").glob("*.yaml")):
        scenario = yaml.safe_load(path.read_text(encoding="utf-8"))
        examples.append(mask_any({"inputs": {"scenario_id": scenario["id"], "request": scenario["request"]},
                                  "outputs": scenario.get("expect", {}),
                                  "metadata": {"scenario_id": scenario["id"], "source": path.name}}))
    return examples


def push_dataset(client: Any, examples: list[dict], *, name: str = "orchestra-scenarios",
                 tag: str | None = None) -> Any:
    """Create/read a named dataset, upload examples, and optionally version-tag it."""
    dataset = sdk_call(client, "read_dataset", dataset_name=name)
    if dataset is None:
        dataset = sdk_call(client, "create_dataset", name)
    if dataset is None:
        return None
    result = sdk_call(client, "create_examples", dataset_name=name, examples=mask_any(examples))
    if tag:
        sdk_call(client, "update_dataset_tag", dataset_name=name, as_of=datetime.now(timezone.utc), tag=tag)
    return result


def from_runs(runs: list[Any], *, score_key: str, threshold: float = 0.5) -> list[dict]:
    """Select low-feedback production runs, retaining the originating run ID."""
    selected = []
    for run in runs:
        score = (getattr(run, "feedback_stats", {}) or {}).get(score_key, {}).get("avg")
        if score is not None and score < threshold:
            selected.append(mask_any({"inputs": run.inputs or {}, "outputs": run.outputs or {},
                                      "metadata": {"source_run_id": str(run.id)}}))
    return selected
