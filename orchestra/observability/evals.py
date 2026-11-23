"""오프라인 평가 실행 (DESIGN §9 #11). 같은 target/evaluators 로 ① 로컬 실행(네트워크 없음, CI 게이트) ② LangSmith 실험 둘 다 지원."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

from ..simulation import run_scenario
from .datasets import scenario_examples
from .evaluators import EVALUATORS, SUMMARY_EVALUATORS, summarize


def target(inputs: dict) -> dict:
    """평가 대상: 시나리오 id 로 실제 그래프를 실행하고 요약을 반환. 예외는 요약에 담아(=실패 점수) 실험이 죽지 않게 한다."""
    try:
        res = run_scenario(inputs["scenario_id"], approve=list(inputs.get("approve") or []))
        return summarize(res)
    except Exception as exc:  # noqa: BLE001
        return {"scenario_id": inputs.get("scenario_id"), "error": f"{type(exc).__name__}: {exc}",
                "expect_errors": [f"target crashed: {exc}"], "invariants": {}}


@dataclass
class LocalReport:
    rows: list[dict[str, Any]] = field(default_factory=list)       # {scenario_id, scores:{key:(score,comment)}}
    summary: dict[str, dict] = field(default_factory=dict)

    @property
    def failures(self) -> list[str]:
        out = [f"{r['scenario_id']}:{k} {c}" for r in self.rows for k, (s, c) in r["scores"].items() if s < 1.0]
        out += [f"summary:{k} {v['comment']}" for k, v in self.summary.items() if v["score"] < 1.0]
        return out

    @property
    def passed(self) -> bool:
        return bool(self.rows) and not self.failures


def run_local(examples: Optional[list[dict]] = None) -> LocalReport:
    """LangSmith 없이 평가자 전부를 실행하고 합격 여부를 계산 (CI 게이트용). 예제가 없으면 불합격."""
    examples = scenario_examples() if examples is None else examples
    rep, outs, refs = LocalReport(), [], []
    for ex in examples:
        out = target(ex["inputs"])
        ref = ex["outputs"]
        scores = {}
        for ev in EVALUATORS:
            r = ev(ex["inputs"], out, ref)
            scores[r["key"]] = (r["score"], r["comment"])
        rep.rows.append({"scenario_id": ex["inputs"]["scenario_id"], "scores": scores})
        outs.append(out); refs.append(ref)
    for sev in SUMMARY_EVALUATORS:
        r = sev(outs, refs)
        rep.summary[r["key"]] = r
    return rep


def to_sdk_examples(examples: list[dict]) -> list[Any]:
    """dict 예제 → `langsmith.schemas.Example` (SDK 의 evaluate 는 서버 없이도 Example 객체를 요구한다).
    id 는 scenario_id 에서 결정적으로 만들어 실행 간 비교가 가능하다."""
    import uuid
    from datetime import datetime, timezone

    from langsmith.schemas import Example
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [Example(id=uuid.uuid5(uuid.NAMESPACE_URL, f"orchestra/{e['metadata']['scenario_id']}"),
                    inputs=e["inputs"], outputs=e["outputs"], metadata=e["metadata"], created_at=now,
                    dataset_id=uuid.uuid5(uuid.NAMESPACE_URL, "orchestra/orchestra-scenarios"))
            for e in examples]


def run_experiment(examples: Optional[list[dict]] = None, *, prefix: str = "orchestra-eval",
                   client: Any = None, max_concurrency: int = 1) -> Any:
    """LangSmith `evaluate` 실험 실행 — **업로드 전용**. LANGSMITH_API_KEY 또는 명시적 client 가 필요하다.

    무네트워크 평가는 `run_local` 을 쓴다. SDK 의 `evaluate` 는 `upload_runs=False`·tracing off·루프백 클라이언트로도
    내부 run 전송 시도/재시도를 멈추지 않았다(실측: 5분 타임아웃) → 오프라인 모드를 SDK 에 맡기지 않는다.
    마스킹/closed 보안 모드에서는 호출하지 않는 것이 원칙이다(트레이스가 외부로 나감). `[VERIFY]` 실제 키로 1회 실행 필요."""
    from langsmith import evaluate
    if client is None and not os.environ.get("LANGSMITH_API_KEY"):
        raise RuntimeError("run_experiment uploads to LangSmith: set LANGSMITH_API_KEY or pass client "
                           "(use run_local() for offline evaluation)")
    examples = scenario_examples() if examples is None else examples
    kwargs: dict[str, Any] = dict(data=to_sdk_examples(examples), evaluators=EVALUATORS,
                                  summary_evaluators=SUMMARY_EVALUATORS, experiment_prefix=prefix,
                                  max_concurrency=max_concurrency, metadata={"suite": "orchestra-scenarios"})
    if client is not None:
        kwargs["client"] = client
    return evaluate(target, **kwargs)


def main(argv: Optional[list[str]] = None) -> int:
    """`python -m orchestra.observability.evals` — 무네트워크 평가. 종료 코드 0 합격 / 1 불합격 (CI 게이트)."""
    rep = run_local()
    for r in rep.rows:
        bad = {k: c for k, (s, c) in r["scores"].items() if s < 1.0}
        print(f"{'FAIL' if bad else 'ok  '} {r['scenario_id']}" + (f"  {bad}" if bad else ""))
    for k, v in rep.summary.items():
        print(f"summary {k}: {v['score']} ({v['comment']})")
    print("eval gate:", "PASS" if rep.passed else "FAIL")
    return 0 if rep.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
