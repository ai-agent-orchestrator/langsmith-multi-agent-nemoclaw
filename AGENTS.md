# AGENTS.md — Codex 작업 규칙

> **먼저 `docs/CODEX_HANDOFF_NEXT.md` (최신 남은 작업)를 읽는다. `CODEX_HANDOFF.md` 는 이전 기준 참고용.** 코어(필터·결제·계층형 그래프·정책 생성)는 이미 구현·테스트되어 있고, 남은 작업과 설계 이탈 목록이 거기 있다. NemoClaw 는 `docs/NEMOCLAW_ROADMAP.md`.

이 저장소는 `docs/DESIGN.md`를 구현한다. 설계서가 단일 기준이다. 설계서와 코드가 충돌하면 코드를 설계서에 맞추고, 설계서가 틀렸다고 판단되면 PR 본문의 "설계 이탈" 섹션에 이유를 적는다.

## 작업 순서
- `docs/CODEX_HANDOFF.md` §2 의 **C1 → C15 순서**로, 항목 1개 = PR 1개. (NemoClaw 는 C12 = ROADMAP N1~N7.) 설계서 §16 의 M2~M5 코어는 완료됨.
- 각 PR 시작 전에 §1(현재 코드 진단)과 해당 절을 다시 읽는다.
- `[VERIFY]` 표시가 있는 API/CLI는 구현 전에 설치된 버전에서 확인한다(`pip show`, `python -c "import ...; help(...)"`, `<bin> --help`). 확인 결과를 PR 본문 "검증 기록" 표에 남긴다. 확인할 수 없으면 기능 탐지 + 우아한 강등으로 구현한다.

## 절대 규칙
1. 기존 테스트 60개(`tests/test_observability.py` 14 + 코어 46)는 수정하지 않고(현재 전체 193개) 계속 통과시킨다. 코어 동작을 바꿔야 하면 PR 본문에 이유를 적는다.
2. 기존 import 경로(`travel_planner.*`, `TravelService`, `build_*_app(model)`)를 깨지 않는다. 옮긴 코드는 shim에서 re-export한다.
3. `orchestra/` 코어는 `domains/*`를 import하지 않는다. (`tests/test_domain_swap.py`가 검사)
4. 모델 ID, LangSmith 엔드포인트, NemoClaw 추론 URL, 결제 호스트를 코드에 하드코딩하지 않는다. 전부 Settings/env.
5. `third_party/` 아래 원본 파일은 수정하지 않는다. 필요한 변경은 어댑터에서 한다. 재벤더링은 `scripts/sync_third_party.py`로만.
6. 서브프로세스는 `shell=False` + 인자 리스트 + timeout. NemoClaw/OpenShell/Claude CLI는 `orchestra/sandbox/nemoclaw/cli.py`, `orchestra/modules/cli_adapter.py`의 allowlist를 통해서만 호출.
7. EXEC 등급 툴(파일 쓰기·셸)은 `ORCHESTRA_SANDBOX=off`에서 실행하지 않고 dry-run diff만 반환한다.
8. COMMIT 등급 툴(결제·확정 예약)은 승인(Approval.decided=True) 없이 실행되는 경로가 있으면 안 된다. `payment_safety` 평가자가 0점이면 CI 실패.
9. 테스트는 LLM·외부 네트워크·NemoClaw 없이 통과해야 한다. 실제 연동은 `-m integration`, `-m langsmith`, `-m nemoclaw` 마커로 분리하고 기본 skip.
10. 비밀값(.env, 키)을 커밋하지 않는다. 로그·트레이스·예외 메시지에 원문 PII/PCI가 남지 않게 마스킹 경로를 거친다.

## 코드 스타일
- Python 3.11+, pydantic v2, 타입 힌트 필수, `ruff` + `mypy orchestra` 통과.
- 공개 함수는 docstring 한 줄 이상(한국어 가능). 주석은 "왜"만.
- 순수 로직(필터 결정, 상태기계, 팬아웃 정규화, 정책 생성)은 그래프/SDK 의존 없이 분리해 단위 테스트.
- 새 LangSmith 호출은 `orchestra/observability/*`의 `safe_call()`을 거친다(장애가 앱 실패로 번지지 않게).

## 매 PR 완료 조건
```bash
ruff check . && mypy orchestra
pytest -q
python -I examples/simulate_hierarchical.py --mode print            # M5 이후
python -I examples/simulate_hierarchical.py --mode graph --sandbox fake --security-mode closed   # M5 이후
python -m orchestra.observability.showcase all --offline            # M7 이후
python scripts/gen_nemoclaw_presets.py --check                      # M8 이후
python scripts/sync_third_party.py --check                          # M1 이후
```
그리고 해당 마일스톤 DoD(설계서 §16 표).

## PR 본문 템플릿
```
## 무엇을
## 설계서 대응 절 (예: §6.4, §11.4)
## 검증 기록 ([VERIFY] 항목: 확인한 버전 / 실제 API·CLI / 결정)
## 설계 이탈 (없으면 "없음")
## 실행 결과 (위 완료 조건 명령의 요약 출력)
```

## 참고 원본
- 기본 뼈대: `ai-agent-orchestrator/langsmith-multi-agent-nemoclaw` (main)
- Claude 플러그인 14개: `ai-agent-orchestrator/claude-10-plugin-for-multi-agent` → `third_party/claude_plugins/`
- 모듈 6개: `ai-agent-orchestrator/multi-agent-module-lib` → `third_party/multi_agent_module_lib/` (플러그인 저장소 `examples/`와 동일 파일이므로 이쪽만 벤더링)
