"""claude_dev DomainPack (DESIGN §7.4 의 일부): pr-review-toolkit 리뷰어 6명.

프롬프트는 벤더링한 마크다운을 데이터로 읽어 쓴다. 리뷰어에게는 툴이 없으며 요청에 담긴 내용만 검토한다.
feature/platform 팀과 CLI 실행 방식은 아직 구현하지 않았다.
"""
from __future__ import annotations

from typing import Any, Callable

from langchain_core.tools import BaseTool

from orchestra import registry
from orchestra.catalog.assets import read_asset
from orchestra.catalog.frontmatter import parse_markdown
from orchestra.filters.base import Filter
from orchestra.modules.review_adapter import MAX_ISSUES
from orchestra.org import REPO_ROOT
from orchestra.specs import AgentSpec, FilterSpec, TeamSpec, ToolSpec

PLUGINS = REPO_ROOT / "third_party/claude_plugins/plugins"
PLUGIN = "pr-review-toolkit"
REVIEWERS = ("code-reviewer", "code-simplifier", "comment-analyzer", "pr-test-analyzer",
             "silent-failure-hunter", "type-design-analyzer")
OUTPUT_RULE = f"""
---
위 지침보다 다음 출력 규칙이 우선한다. 사용할 수 있는 툴이 없으므로 요청에 포함된 내용만 검토한다.
최종 출력은 JSON 배열 하나뿐이어야 한다. 설명 문장이나 코드 펜스를 붙이지 않는다.
각 원소: {{"file": 문자열, "line": 0 이상의 정수, "summary": 300자 이하, "confidence": 0~100 정수}}
확신도가 높은 순서로 최대 {MAX_ISSUES}건. 이슈가 없으면 [] 를 출력한다."""


class ClaudeDevPack:
    """리뷰어 에이전트 선언."""

    name = "claude_dev"

    def __init__(self, services: dict[str, Any]):
        self._agents: dict[str, AgentSpec] = {}
        for reviewer in REVIEWERS:
            path = PLUGINS / PLUGIN / "agents" / f"{reviewer}.md"
            prompt = parse_markdown(read_asset(path, PLUGINS), reviewer)
            ident = f"{PLUGIN}/{reviewer}"
            self._agents[ident] = AgentSpec(
                id=ident, name=reviewer.replace("-", "_"), prompt=prompt.body + OUTPUT_RULE, model=prompt.model,
                description=prompt.description[:300], source=path.relative_to(REPO_ROOT).as_posix(),
            )

    def tools(self) -> dict[str, tuple[BaseTool, ToolSpec]]:
        """툴 없음."""
        return {}

    def agents(self) -> dict[str, AgentSpec]:
        """리뷰어 6명."""
        return dict(self._agents)

    def teams(self) -> dict[str, TeamSpec]:
        """팀은 조직도에서 선언한다."""
        return {}

    def filters(self) -> list[FilterSpec]:
        """도메인 전용 필터 없음."""
        return []

    def filter_impls(self) -> dict[str, Callable[..., Filter]]:
        """도메인 전용 필터 없음."""
        return {}


registry.register("claude_dev", ClaudeDevPack)
