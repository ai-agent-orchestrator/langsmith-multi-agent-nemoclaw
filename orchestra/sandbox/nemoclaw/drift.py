"""기동 시 정책 드리프트 점검 (R2 / NS1). 불일치면 기동 실패(fail-closed).

`fetch_applied` 는 샌드박스별 "실제 적용된 preset 이름 목록" 을 돌려주는 함수다.
실제 구현은 NemoClawRunner.applied_presets (CLI 조회) — `[VERIFY]` 출력 형식은 R0 에서 확정.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable

from ...specs import ToolSpec
from . import policy


class PolicyDriftError(RuntimeError):
    def __init__(self, problems: list[str]):
        super().__init__("policy drift: " + "; ".join(problems))
        self.problems = problems


def expected_state(tool_specs: Iterable[ToolSpec], *, security_mode: str) -> tuple[dict, dict[str, list[str]]]:
    specs = list(tool_specs)
    presets = policy.build_presets(specs, security_mode=security_mode)
    sandboxes = sorted({s.sandbox_name for s in specs if s.sandbox_name})
    return presets, policy.applied_presets(sandboxes, presets)


def verify_startup(tool_specs: Iterable[ToolSpec], fetch_applied: Callable[[str], list[str]], *,
                   security_mode: str = "masked", preset_dir: Path | None = None) -> None:
    """검사 순서: ① 생성된 preset 자체의 규칙 위반 ② 저장소 preset 파일과 선언의 diff ③ 실제 적용 상태 비교."""
    specs = list(tool_specs)
    presets, expected = expected_state(specs, security_mode=security_mode)
    problems = list(policy.validate_presets(presets, tool_specs=specs))
    if preset_dir is not None:
        problems += [f"file drift: {d}" for d in policy.diff_dir(presets, preset_dir)]
    actual: dict[str, list[str]] = {}
    for sb in expected:
        try:
            actual[sb] = list(fetch_applied(sb))
        except Exception as exc:                      # 조회 실패 = 확인 불가 = 기동 불가
            problems.append(f"{sb}: cannot read applied policy ({type(exc).__name__}: {exc})")
    problems += policy.check_applied(expected, {k: v for k, v in actual.items()})
    if problems:
        raise PolicyDriftError(problems)
