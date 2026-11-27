# 다음 세션 인수인계 (2026-11-27)

> 새 세션은 이 문서부터 읽는다. 그다음 `AGENTS.md`(절대 규칙 10개), `docs/NEMOCLAW_REALITY.md`, `docs/NEMOCLAW_KNOWN_ISSUES.ko.md` 순서.
> 원칙: **한 번에 한 단계.** 각 단계는 완료 조건을 통과한 뒤 커밋·푸시하고 다음으로 넘어간다.

## 0. 현재 상태

| 항목 | 상태 |
|---|---|
| 작업 브랜치 | `21-nemoclaw-cli` (main 위). 멈췄던 로컬 작업을 `ai-security-architecture` 저장소의 `reference-implementation/` 스냅샷(SHA-256 검증)에서 복원 |
| NemoClaw 실설치 | 사용자 PC, WSL2 Ubuntu 26.04 + Docker Desktop. nemoclaw v0.0.145, openshell 0.0.116, 샌드박스 `my-assistant`(OpenClaw, ollama `qwen3:1.7b`, GPU) |
| 실측 완료 | CLI 문법, `openshell policy get --base` 출력 형식, `exec`(약 1초, `uid=998(sandbox)`, Python 3.13.5), 우리 프리셋 2개 dry-run 통과 → `docs/NEMOCLAW_REALITY.md` §3 |
| 해결한 장애 | 이전 WSL 부팅이 남긴 샌드박스 잠금 때문에 nemoclaw 샌드박스 명령이 전부 멈춤 → `scripts/nemoclaw_lock_doctor.sh --clean`으로 해결(45초 이상 → 1.15초) |
| 남은 환경 문제 | 샌드박스 추론 HTTP 502(Ollama 인증 프록시 미기동). 개발엔 영향 없음(ScriptedChatModel). 실제 대화가 필요하면 사용자가 `nemoclaw my-assistant connect` |
| 테스트 | **446 통과 / 실패 0** (2개 제외 마커), Python 3.11 + `.[dev]` 새 환경에서 확인. 1단계 시작 전에는 319 통과 / 22 실패 + 1 파일 import 오류 |
| 정적 검사 | `ruff` 통과, `mypy orchestra` 오류 0 |

## 1단계 — 복원 마무리: 실패 테스트 22개 + import 오류 1개 (완료)

> **완료 (2026-11-27).** 다음 작업은 2단계다. 아래 표는 당시 명세 기록이다.
> 빠진 파일의 원본을 찾지 못해 테스트를 명세로 새로 구현했다. 설계(`docs/DESIGN.md`)와 다른 점:
> - 샌드박스(`nemoclaw*`) 모드의 실제 모델은 호스트가 `inference.local` 인 openai_compatible 만 허용하고, 샌드박스 밖 openai_compatible 은 loopback 만 허용한다.
> - 승인은 `True` / `{"approved": True}` 만 인정한다. 서명 도입 전에 기록된 승인은 다시 받아야 한다.
> - 감사 로그는 식별자 형태의 정해진 필드만 원문으로 남기고 나머지는 digest 로 남긴다. 감사 DB 경로용 환경변수는 없다(`services["audit_log"]` 로 주입).
> - 팀 회로 차단기는 자동 분할 전 선언 팀 기준으로 묶는다. 오프라인 평가는 SDK 를 호출하지 않는다.
> - `claude_dev` 는 리뷰 팀만 있다(§7.4 의 glob 확장, feature/platform 팀, cli 실행 방식 미구현). 리뷰 결과 feedback `review_kept_ratio` 는 없다.
> - `concert_payment` 조직과 도메인 교체 테스트(§12.3 보너스의 결제 부분)는 만들지 않았다.

실패 테스트가 곧 명세다. **테스트를 고치지 말고 코드를 채운다.** 기존 통과 테스트는 수정 금지(AGENTS.md 규칙 1).

| 빠진 것 | 근거가 되는 테스트 |
|---|---|
| `orchestra/models/providers.py` — `ProviderBuilder(env, constructors=…)` | `tests/test_security_foundation.py` (import 오류로 파일 전체가 안 돎) |
| `orchestra/modules/loop_adapter.py` — `loop_node(...)` | `domains/security/pack.py` 가 import, 같은 파일 |
| `ExecRequest.agent` 필드 (`orchestra/sandbox/base.py`) | `tests/test_nemoclaw_runtime.py` 5개 |
| 승인 서명 (`ApprovalRecord.signature`, `signature_algorithm == "hmac-sha256"`, 키 ID) | `tests/test_security_integration.py::test_approval_signature_tampering_and_cross_store_replay` |
| 런타임 감사 훅 (중첩 인자는 다이제스트로만 기록) | `…::test_runtime_audit_hook_records_nested_arguments_only_as_digest` |
| 전역 kill switch (모델 호출 전에 그래프 중단) | `…::test_global_kill_stops_graph_before_model` |
| `orgs/concert_demo.yaml`, `domains/concert_demo/` | `test_simulation_golden[concert_swap]`, `test_backends_parity[concert_swap]`, `test_langsmith_offline` 2개 |
| `scenarios/pr_review_parallel.yaml` (리뷰어 6명 팬아웃) | `…::test_six_reviewer_fanout_is_normalized_and_aggregates_three_issues` |
| 재귀 실패를 차단 결과로 반환, 오프라인 평가가 SDK 클라이언트를 만들지 않음 | `test_security_integration.py` 나머지 2개 |
| `cli_adapter` 사람 승인은 명시적 `True`만 허용, 실제 하위 프로세스 stdin 왕복 | `tests/test_cli_limits.py` 6개 |
| `FilterChain.security_control` | `mypy` 오류(`orchestra/sandbox/nemoclaw/worker.py:61`) |

참고 자료: `ai-agent-orchestrator/ai-security-architecture` 브랜치 `claude/top-tier-security-roadmap`의 `reference-implementation/`(이미 복원한 파일의 원본), `nemoclaw/21-security-decision-layer` 브랜치(결정 계층·egress 격리·플러그인 도입 게이트).

**완료 조건**
```bash
ruff check . && mypy orchestra
pytest -q                      # 실패 0
python -I examples/simulate_hierarchical.py --mode graph --sandbox fake --security-mode closed
python -m orchestra.observability.showcase all --offline
python scripts/gen_nemoclaw_presets.py --check
python scripts/sync_third_party.py --check
```
작은 단위로 나눠 커밋한다(예: providers+loop_adapter → ExecRequest.agent → 승인 서명 → 감사 훅·kill switch → concert_demo·pr_review 시나리오 → cli_adapter).

## 2단계 — 워커 연결 (N2): 한 번 띄워 계속 쓰는 실행 경로 (오프라인 완료, 워커 실연동 남음)

> **오프라인 구현 완료 (2026-11-27).** 아래 "방향"의 `nemoclaw exec` 상시 실행은 소스 확인 결과 동작하지 않아(입력을 EOF 까지 모음, 실행 내내 잠금 보유) **OpenShell SSH**로 바꿨다. 근거와 실설치에서 남은 확인은 [`NEMOCLAW_REALITY.md`](NEMOCLAW_REALITY.md) §3.1.
> - 구현: `orchestra/sandbox/nemoclaw/persistent.py`, `ssh_launcher.py`, 워커 `--serve`. 기본값은 기존 1회 실행이며 `NEMOCLAW_TRANSPORT=ssh_stream`으로 켠다. 1회 실행 명령에는 `--stdin`을 추가했다.
> - 완료 조건: `tests/test_nemoclaw_persistent.py`(실프로세스 오프라인 왕복 + `-m nemoclaw` 실연동 1개, 기본 skip).
> - 실설치 SSH 점검: 통과(2026-11-27, `scripts/nemoclaw_ssh_stream_check.sh`, REALITY §3.1).
> - 설치 준비 완료(실설치 미실행): `nemoclaw onboard --from`은 워커 이미지에 쓸 수 없어(REALITY §3.2) `scripts/nemoclaw_worker_sandbox.py`가 `openshell sandbox create --from`을 쓴다. `docs/DESIGN.md`의 `onboard --from` 서술과 다르다. 키는 샌드박스별 파생 키.
> - 남음: (사용자 승인 후) 샌드박스 생성 → 실연동 테스트. `orchestra-code`에서 실행할 샌드박스 툴 정의.
> - 아래는 당시 명세 기록이다.

- 이유: nemoclaw 호출은 무겁고 호스트 잠금으로 직렬화된다. 툴 호출마다 `exec`를 새로 띄우면 멀티 에이전트가 사실상 멈춘다(KNOWN_ISSUES §2, §4).
- 방향: `nemoclaw <sb> exec --stdin -- /opt/orchestra/venv/bin/python -m orchestra.sandbox.nemoclaw.worker`를 **한 번** 띄우고, 줄 단위 JSON 요청·응답을 주고받는 지속형 transport. 재시작·타임아웃·크기 상한·fail-closed. `exec`는 `--stdin`을 명시해야 파이프가 전달된다(실측).
- 필요: 샌드박스 이미지에 워커 설치(`nemoclaw/Dockerfile.worker` 참고, `nemoclaw onboard --from` 또는 `upload`). **이미지 재생성은 사용자 승인 후.**
- 완료 조건: fake 프로세스로 왕복 테스트(오프라인) + `-m nemoclaw` 마커 실연동 테스트 1개(기본 skip).

## 3단계 — 샌드박스 안 2차 필터 (N3)
워커가 받은 요청에 pre_tool/post_tool 필터를 다시 적용한다. 호스트 체인을 우회해도 막힌다. 근거 테스트: `test_nemoclaw_runtime.py::test_worker_rechecks_pre_post_and_commit_without_host_chain`.

## 4단계 — 결정 계층 병합
`nemoclaw/21-security-decision-layer`의 `decision.py`(allow/ask/deny + ReasonCode + FailClosedClient), `egress_isolation.py`, `intake.py`를 이 브랜치로 가져와 흩어진 차단 경로를 하나의 계약으로 묶는다.

## 5단계 — 정책 정리 (사용자 승인 필요)
- `brew-balanced` 제거 검토(`access: full` + `tls: skip`): `nemoclaw my-assistant policy remove brew-balanced --dry-run` → 승인 후 `--yes`.
- 우리 프리셋 1개를 승인된 경로(`orchestra/security/policy_change.py`)로 적용 → `openshell policy get --base`로 커스텀 키 형식(REALITY ⚠️1b) 확인 → `drift_errors` 기대값 확정.

---

## 작업 환경 주의 (반드시 읽기)

- **실행 위치:** 사용자 PC 는 Windows. NemoClaw 는 **WSL2 안**에 있다. 사용자에게 명령을 줄 때는 PowerShell 에서 `bash -lc "…"`로 실행하게 하거나 WSL 셸 안에서 실행하게 한다.
- **PowerShell 함정:** `~`는 확장되지 않는다(git clone 이 `system32\~`에 생긴 적 있음). `$!`, 중첩 따옴표가 깨진다 → 긴 명령은 스크립트 파일로. 사용자 체크아웃이 CRLF 일 수 있어 스크립트는 `bash -c "tr -d '\r' < scripts/X.sh > scripts/X_lf.sh && bash -l scripts/X_lf.sh"`로 실행.
- **nemoclaw 명령을 동시에 돌리지 않는다.** 호스트 잠금 경합으로 둘 다 실패한다.
- **샌드박스 명령이 말없이 멈추면** 먼저 `scripts/nemoclaw_lock_doctor.sh`(읽기 전용) → 필요 시 `--clean`.
- **Docker Desktop 이 꺼져 있으면** 게이트웨이가 다운된다 → 켜고 `nemoclaw my-assistant recover`.
- **바꾸면 안 되는 것:** `policy set` 금지(전체 교체). 정책 변경·이미지 재생성·프리셋 제거는 사용자 승인 후. 비밀값·`.env` 커밋 금지. 사용자 PC 경로나 사용자명 같은 개인 정보는 공개 저장소 문서에 쓰지 않는다.
- **점검 도구:** `scripts/nemoclaw_probe.sh`(읽기 전용 실측), `nemoclaw_hang_diag.sh`(멈춤 지점), `nemoclaw_lock_doctor.sh`(잠금). 결과는 사용자 Windows 폴더 `nemoclaw-probe\`에 저장된다.
- **PR 규칙:** `AGENTS.md`의 PR 본문 템플릿(무엇을 / 설계서 대응 절 / 검증 기록 / 설계 이탈 / 실행 결과).
