"""시나리오 → LangSmith 데이터셋 예제 (DESIGN §9 #9). 순수 변환 + 멱등 업로드.

예제의 inputs 는 *어떤 시나리오를 돌릴지*(scenario_id)와 요청 정보만 담는다. 스크립트(스크립트된 모델 응답)는
저장소가 소유하므로 데이터셋에 넣지 않는다 → 데이터셋이 코드와 어긋나지 않고, PII 도 데이터셋에 들어가지 않는다.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from ..simulation import list_scenarios, load_scenario
from .safe import safe_call

DATASET_NAME = "orchestra-scenarios"


def category(sid: str) -> str:
    if sid.startswith("redteam_"):
        return "redteam"
    if sid.startswith("security_"):
        return "security"
    return "core"


def scenario_examples(ids: Optional[Iterable[str]] = None) -> list[dict[str, Any]]:
    out = []
    for sid in (list(ids) if ids is not None else list_scenarios()):
        sc = load_scenario(sid)
        out.append({
            "inputs": {"scenario_id": sc["id"], "request": sc["request"], "approve": sc.get("approve", [True])},
            "outputs": {"expect": sc.get("expect", {})},
            "metadata": {"scenario_id": sc["id"], "category": category(sc["id"]), "title": sc.get("title", "")},
        })
    return out


def push_dataset(client: Any, examples: list[dict[str, Any]], name: str = DATASET_NAME) -> dict[str, int]:
    """멱등 업로드: 데이터셋이 없으면 만들고, 이미 있는 scenario_id 는 건너뛴다. 반환: {"created": n, "skipped": m}.
    LangSmith 장애 시 {"created": 0, "skipped": 0, "error": 1} (앱을 막지 않음)."""
    if not safe_call(client.has_dataset, dataset_name=name, default=False):
        ds = safe_call(client.create_dataset, name, description="Orchestra scenarios (generated from scenarios/*.yaml)")
        if ds is None:
            return {"created": 0, "skipped": 0, "error": 1}
    existing = {(e.metadata or {}).get("scenario_id")
                for e in (safe_call(lambda: list(client.list_examples(dataset_name=name)), default=[]) or [])}
    fresh = [e for e in examples if e["metadata"]["scenario_id"] not in existing]
    if fresh:
        safe_call(client.create_examples, dataset_name=name,
                  inputs=[e["inputs"] for e in fresh], outputs=[e["outputs"] for e in fresh],
                  metadata=[e["metadata"] for e in fresh])
    return {"created": len(fresh), "skipped": len(examples) - len(fresh)}
