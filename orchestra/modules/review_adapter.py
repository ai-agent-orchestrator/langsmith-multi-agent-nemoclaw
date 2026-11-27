"""벤더 `review_aggregator.aggregate` 를 병렬 팀의 reducer 로 감싼다 (원본은 수정하지 않는다).

리뷰어 출력은 신뢰하지 않는 데이터다. 형식이 어긋나면 해당 리뷰어를 invalid_output 으로 표시하고
결과 전체를 complete=false 로 만든다.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from third_party.multi_agent_module_lib.review_aggregator import Issue, aggregate

MAX_ISSUES = 8
# 메시지 계약이 32768자를 넘는 메시지를 차단하므로 여유를 두고 줄인다.
MAX_DOCUMENT = 30_000


class ReviewIssue(BaseModel):
    """리뷰어가 보고하는 이슈 한 건."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    file: str = Field(min_length=1, max_length=256)
    line: int = Field(ge=0)
    summary: str = Field(min_length=1, max_length=300)
    confidence: int = Field(ge=0, le=100)


_ISSUES: TypeAdapter[list[ReviewIssue]] = TypeAdapter(Annotated[list[ReviewIssue], Field(max_length=MAX_ISSUES)])


def _report(reviewer: str, issues: list[ReviewIssue] | None) -> dict[str, Any]:
    status = "invalid_output" if issues is None else "ok"
    return {"reviewer": reviewer, "status": status, "issues": [i.model_dump() for i in issues or []]}


def reviewer_reports(reviewer: str, content: Any) -> list[dict[str, Any]]:
    """리뷰어의 JSON 배열 출력을 검증해 보고서 한 건으로 만든다."""
    try:
        if not isinstance(content, str):
            raise ValueError("review output must be text")
        return [_report(reviewer, _ISSUES.validate_json(content.strip()))]
    except ValueError:
        return [_report(reviewer, None)]


def child_reports(team: str, content: Any) -> list[dict[str, Any]]:
    """하위 병렬 팀 문서에서 원본 보고서를 꺼낸다. 상위 팀이 한 번에 다시 합치기 위해서다."""
    try:
        reports = []
        for item in json.loads(content)["reports"]:
            issues = [ReviewIssue.model_validate(i) for i in item["issues"]]
            report = _report(str(item["reviewer"]), issues)
            if item["status"] != "ok":
                report["status"] = str(item["status"])
            reports.append(report)
        return reports
    except (ValueError, KeyError, TypeError):
        return [_report(team, None)]


def _kept(reports: list[dict[str, Any]]) -> list[Issue]:
    # 합의 가산점은 서로 다른 리뷰어에게만 준다: 한 리뷰어가 같은 위치를 반복하면 한 건으로 센다.
    best: dict[tuple[str, str, int], Issue] = {}
    for report in reports:
        for item in report["issues"]:
            key = (report["reviewer"], item["file"], item["line"])
            if key not in best or item["confidence"] > best[key].confidence:
                best[key] = Issue(item["file"], item["line"], item["summary"], item["confidence"], key[0])
    return aggregate(best.values())


def review_document(reports: list[dict[str, Any]]) -> str:
    """보고서들을 컷오프·중복 제거해 `{"reports", "issues", "dropped", "complete"}` JSON 으로 만든다.

    문서가 너무 크면 확신도가 낮은 원본 이슈부터 빼고 complete=false 로 표시한다.
    """
    reports = [{**r, "issues": list(r["issues"])} for r in reports]
    total = sum(len(r["issues"]) for r in reports)
    complete = bool(reports) and all(r["status"] == "ok" for r in reports)
    while True:
        kept = _kept(reports)
        document = json.dumps({"reports": reports, "issues": [asdict(i) for i in kept],
                               "dropped": total - len(kept), "complete": complete}, ensure_ascii=False)
        remaining = [(item["confidence"], n, i) for n, r in enumerate(reports) for i, item in enumerate(r["issues"])]
        if len(json.dumps(document, ensure_ascii=False)) <= MAX_DOCUMENT or not remaining:
            return document
        _, n, i = min(remaining)
        del reports[n]["issues"][i]
        complete = False
