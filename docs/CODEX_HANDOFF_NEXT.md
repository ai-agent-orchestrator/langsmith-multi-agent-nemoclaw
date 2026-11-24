# CODEX_HANDOFF_NEXT — 남은 개발 전체 (최신)

> 이 문서가 **최신 인수인계서**다. 기존 [`CODEX_HANDOFF.md`](CODEX_HANDOFF.md) 는 보안 작업 이전 기준이라 *C번호·설계 이탈 참고용*으로만 본다. 충돌하면 이 문서와 코드가 우선.
> 작성 시점: 2026-11-24 · 테스트 **193개 통과** (기존 코어 60개는 수정 금지).

## 0. 시작하기
```bash
pip install -r requirements.txt && pytest -q          # 193 pass 확인
python -I examples/simulate_hierarchical.py --mode graph --sandbox fake --security-mode closed
python scripts/gen_nemoclaw_presets.py --check
python scripts/security_gate.py --help
python -m orchestra.observability.evals               # 오프라인 평가 게이트
```
작업 규칙은 [`AGENTS.md`](../AGENTS.md) (절대 규칙 10개). 1항목 = 1 PR, 브랜치 `NN-이름`.
읽을 순서: `NEMOCLAW_REALITY.md` → `SECURITY_ROADMAP.md` → `NEMOCLAW_ROADMAP.md` → `DESIGN.md`.

## 1. 이미 끝난 것 (다시 만들지 말 것)
| 영역 | 위치 |
|---|---|
| 계층형 supervisor, OrgSpec, 팬아웃 정규화, DomainPack 레지스트리 | `orchestra/{org,specs,registry,runtime,patterns}` |
| 5단계 FilterChain(fail-closed), `code_guard` | `orchestra/filters/**` |
| HITL(`interrupt`), 승인 원장, COMMIT 마지막 방어선 | `orchestra/hitl.py`, tool wrapper |
| 서비스 + `GraphRecursionError`→blocked(`recursion_limit`) | `orchestra/service.py` |
| ScriptedChatModel 시뮬레이션, 시나리오 YAML 골든 | `orchestra/simulation.py`, `scenarios/` |
| 도메인: travel, payment, **security(security_lead)** | `domains/`, `orgs/security_review.yaml` |
| NemoClaw 정책 생성/검증(공식 JSON 스키마), **Runner(R1)**, **드리프트 가드(R2)** | `orchestra/sandbox/nemoclaw/`, `vendor/nemoclaw-schemas/` |
| 보안 하네스: Scanner 계약(R3), 불변식 4종, garak 브리지, 해시체인+HMAC 감사로그, `security_gate.py`, CI | `orchestra/security/`, `scripts/`, `.github/workflows/security.yml` |
| LangSmith 평가: 데이터셋, 평가자, `run_local`(오프라인 게이트), `run_experiment`(업로드 전용) | `orchestra/observability/` |
| 문서/학습: REALITY, 보안·평가 스터디, 개념 정리 | `docs/` |

## 2. 남은 작업 (우선순위 순)

### P0 — 실환경 검증 (코드보다 먼저, 결과를 문서에 반영)
| # | 작업 | 완료 조건 |
|---|---|---|
| **V1** | NemoClaw 실제 설치 후 `NEMOCLAW_REALITY.md` §3 체크리스트 전부 확인: `policy get` 출력 샘플, `--from-file` 의 프리셋 이름 결정 규칙, `binaries` glob 적용 여부, LangSmith 경로 규칙, **샌드박스 내부 exec 방법**(`nemoclaw <sb> exec`? 후보), OpenShell 버전 | 표의 ⚠️/[VERIFY] 모두 ✅ 또는 ❌+대안으로 갱신, 샘플 출력은 `tests/fixtures/` 에 저장 |
| **V2** | `parse_presets_from_policy` 를 V1 의 실제 출력 fixture 로 테스트 | fixture 기반 테스트 추가 |
| **V3** | garak 전체 프로브(`promptinject, encoding, xss`) 실행 → `security_gate.py` 통과 | 실행 로그/리포트 샘플 + 임계값 조정 근거 PR 본문 |
| **V4** | LangSmith 실키로 `run_experiment` 업로드 경로 검증(`-m langsmith`) | 실험이 UI에 보임, 스크린샷/URL PR 본문 |

### P1 — NemoClaw 실행 계층 (NEMOCLAW_ROADMAP N2~N7 / SECURITY_ROADMAP NS1~NS9)
| # | 작업 | 메모 |
|---|---|---|
| **N2** | Worker + Transport: Runner 의 `exec()` 실제 경로 구현(V1 결과 따라) | 지금은 transport 없으면 `sandbox_unavailable` |
| **N3** | 샌드박스 내부에서도 FilterChain 이중 검사(pre_tool/post_tool) | 호스트 우회 시에도 차단 |
| **N4** | 추론은 `inference.local` + privacy router 경유만 허용 | 모델 호스트 하드코딩 금지 |
| **N5** | egress deny 이벤트 → LangSmith feedback + annotation queue | `safe_call` 필수 |
| **N6** | 코드 샌드박스 `orchestra-code`(EXEC 툴 전용, 파일시스템/프로세스 레이어는 **재생성 필요**함을 문서화) | 이름 규칙 `SANDBOX_NAME_RE` |
| **N7** | `Dockerfile.worker`, 운영 런북(`docs/OPS_RUNBOOK.md` 신규) | |
| **NS*** | `SECURITY_ROADMAP.md` 의 NS1~NS9 중 드리프트 외 미완 항목 | 각 항목 완료 조건은 해당 문서 |

### P1 — 멀티에이전트 보안 (SECURITY_ROADMAP S1~S8 중 미완)
- **S2 taint / capability token**: 툴 출력(비신뢰)이 COMMIT 인자로 흘러가는 경로 차단.
- **서명된 승인**: 승인을 (tool, args 해시, 금액, 만료)에 바인딩 + 일회성. 현재 원장은 **프로세스 메모리** → 영속 저장소(추상 인터페이스 + SQLite 구현)로.
- **결제 원장(hold 등)도 메모리** → 같은 저장소 인터페이스로.
- **서킷브레이커 / kill switch**: 연속 차단 N회 시 팀 단위 중단, env 로 전역 정지.
- **플러그인 수집 게이트(S3)**: 벤더링 시 SHA 고정 + 스캐너 통과 필수(`sync_third_party.py` 에 연결).
- **Claude Security 어댑터**: 인터페이스 확정 후 `SecurityScanner` 구현체로 추가(R3 계약 `from_audit_json` 재사용). 인터페이스 미확정이면 FakeScanner 로 계약 테스트만.

### P2 — Claude 플러그인 / module-lib 통합 (기존 C1~C10 중 미완)
| # | 작업 |
|---|---|
| **C1** | `pyproject.toml`(extras: dev/langsmith/nemoclaw), ruff/mypy, `.env.example` |
| **C2** | `travel_planner/*` shim + `orgs/travel_legacy.yaml` (기존 import 경로 유지) |
| **C3** | `scripts/sync_third_party.py`(+`--check`), `third_party/`, `LOCK.json`, NOTICE (security-guidance 는 Apache-2.0). **S3 게이트 적용** |
| **C4** | frontmatter 파서(`orchestra/catalog/frontmatter.py`) |
| **C5** | 플러그인 14개 카탈로그, 에이전트 ID `plugin/agent`, `load_skill`(20000자 상한) |
| **C6** | Claude Code 툴 → 로컬 매핑. Read/Grep/Glob 경로 고정(심볼릭 링크 탈출 차단), Write/Edit/Bash=EXEC+sandbox |
| **C7** | `domains/claude_dev` + `orgs/claude_dev.yaml` |
| **C8** | module-lib 어댑터 5종(guardrail→필터, loop_control, review_aggregator, orchestrator, cli_subagent) |
| **C9** | hookify `.local.md` → 필터 변환 |
| **C10** | 시나리오 4개 + `domains/concert_demo/`(코어 수정 0) |

### P2 — LangSmith 나머지 (DESIGN §9.2 의 24개 중 미구현)
tracing span/threads, distributed tracing, feedback 업로드, prompt hub, annotation queue, online eval, OTel, pairwise 평가, `showcase` CLI(`--offline` 필수), `OrchestraService(config_hook, scope)` 연결.
- 주의: **`closed` 보안 모드에서는 SDK `evaluate` 호출 금지**(드리프트 가드가 막음). SDK 0.16 `evaluate` 는 `Example` 객체 필요, 오프라인 불가.

### P3 — 정리 / 확장
- **C13** `simulate_hierarchical.py` 옵션(`--pattern --org --trace`), **C14** playground 탭·`langgraph.json`.
- **C15** `langchain.agents.create_agent` 백엔드(`AgentBackend` 추상화 + parity 테스트). 현재 `create_react_agent` DeprecationWarning ≈1,000건.
- `AliasModelFactory` provider builder 미구현.
- 병렬 팀(Send 팬아웃) 미구현.
- `security-guidance` 플러그인 원본 코드는 복사하지 않음(`code_guard` 규칙은 새로 작성) — C8 에서 합집합 시 라이선스 표기.

## 3. 알려진 제약 / 함정
- `openshell policy set` 은 **전체 정책을 교체**한다 → 코드에서 금지(`CommandTemplates` 에 없음). `policy add --from-file` 만 사용.
- 커스텀 프리셋 live key: `nemoclaw_custom__<preset>__<key>`.
- 샌드박스 이름: 1–19자, 소문자 시작, 하이픈 단독, 영숫자 끝(`SANDBOX_NAME_RE`).
- `binaries` glob 때문에 **격리 단위는 샌드박스**. 팀별 격리가 필요하면 샌드박스를 나눈다.
- 파일시스템/프로세스 정책 변경은 샌드박스 **재생성** 필요, 네트워크/추론은 라이브 변경 가능.
- 감사로그는 스칼라만 원문 보존, 중첩 값은 해시(PII 누출 방지) — 바꾸지 말 것.
- 툴 이벤트는 **실행될 때만** 기록됨(차단된 툴은 이벤트 없음). 시나리오 expect 작성 시 유의.
- `/home/claude/ref/*`(참조 레포)는 비신뢰 읽기 전용. 코드를 복사하지 말고 `third_party/` + LOCK 으로만.

## 4. 불확실한 것 (⚠️ 구현 전 확인)
NemoClaw 실설치 동작(V1 전부), OpenShell 버전, garak 전체 실행, LangSmith 업로드 경로, Claude Security 인터페이스. 확인 못 하면 **기능 탐지 + 우아한 강등**으로 구현하고 PR 본문 "검증 기록"에 남긴다.

## 5. PR 규칙
- 완료 조건: `ruff check . && mypy orchestra && pytest -q` + AGENTS.md 의 명령들 + `python scripts/security_gate.py` (보안 PR).
- 테스트는 LLM/네트워크/NemoClaw 없이 통과. 실연동은 `-m integration|langsmith|nemoclaw` 마커(기본 skip).
- 보안 관련 PR 은 `invariants: true` 시나리오 1개 이상 추가(레드팀 회귀).
- PR 본문: 변경 요약 / 설계 이탈 / 검증 기록 표 / 남은 위험.
