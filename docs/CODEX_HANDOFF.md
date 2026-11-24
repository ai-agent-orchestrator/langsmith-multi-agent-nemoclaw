> ⚠️ **부분 대체됨** — 최신 남은 작업은 [`CODEX_HANDOFF_NEXT.md`](CODEX_HANDOFF_NEXT.md). 이 문서는 C번호/설계 이탈 참고용.

# Codex 인수인계 — 무엇이 끝났고, 무엇을 하면 되는가

> 기준일 2026-11-22 · 기준 커밋: 이 문서가 들어간 커밋 · 설계 원본: [`DESIGN.md`](DESIGN.md) · 작업 규칙: [`../AGENTS.md`](../AGENTS.md)
> **분담 원칙**: 틀리기 쉬운 코어(상태·승인·필터·그래프·정책 검증)는 Claude 가 구현·테스트까지 끝냈고, 규칙이 정해진 나머지(패키징·벤더링·카탈로그·어댑터·LangSmith 명령·NemoClaw CLI 래퍼·문서)는 Codex 가 맡는다.

## 0. 시작하기 (5분)

```bash
pip install -r requirements.txt
python -m pytest -q tests                                  # 60 passed (기존 14 + 신규 46)
python -I examples/simulate_hierarchical.py --list         # 시나리오 5개
python -I examples/simulate_hierarchical.py                # 제주 + 호텔 hold + 승인 결제 (실제 그래프 실행)
python -I examples/simulate_hierarchical.py --mode graph --scenario sandbox_egress_denied
python scripts/gen_nemoclaw_presets.py --check             # preset drift 검사
```

**먼저 읽을 파일 (이 순서)**: `orchestra/specs.py` → `orchestra/filters/chain.py` → `orchestra/filters/tool_wrapper.py` → `orchestra/patterns/hierarchical_graph.py` → `orchestra/simulation.py` → `domains/payment/*` → `tests/test_simulation_golden.py`

## 1. 이미 끝난 것 (Claude, 테스트 포함 — 다시 만들지 말 것)

| 영역 | 파일 | 핵심 보장 | 테스트 |
|---|---|---|---|
| 스펙 | `orchestra/specs.py` | ToolSpec(부작용 등급·egress·sandbox)/AgentSpec(툴≤3)/TeamSpec/OrgSpec/FilterSpec 검증 | `test_specs_org.py` |
| 조직도 | `orchestra/org.py` | YAML 로드, 구조 검증(사이클·부모 2개·이름 충돌·**팀 경계 넘는 handoff 금지**), **팬아웃 자동 분할**(재귀, 입력 불변), recursion_limit 계산 | `test_specs_org.py` |
| 필터 | `orchestra/filters/{base,chain}.py` | 5단계, order, MODIFY 전파, BLOCK 단락, **승인 지연**(뒤 필터가 BLOCK 하면 사용자에게 묻지 않음), **예외=BLOCK(fail-closed)**, glob 대상 | `test_filters.py` |
| 내장 필터 | `orchestra/filters/builtin/` | request_gate, pii_mask(PCI 포함, 구조 보존), side_effect_policy, budget | `test_filters.py` |
| 툴 래퍼 | `orchestra/filters/tool_wrapper.py` | pre_tool → 승인(interrupt) → 샌드박스/직접 → post_tool. **COMMIT 은 필터 설정과 무관하게 승인 장부 없이는 실행 불가(마지막 방어선)**, EXEC 은 샌드박스 off 면 dry-run, nemoclaw 모드에서 러너 없으면 fail-closed | `test_simulation_golden.py` |
| HITL | `orchestra/hitl.py`, `runtime.py` | 승인 1회용, 재개 시 노드 재실행에도 **결제 정확히 1회**, 무응답=거절 | `test_simulation_golden.py` |
| 모델 | `orchestra/models/{scripted,factory}.py` | ScriptedChatModel(역할별 큐, 바인딩 안 된 툴 호출 즉시 오류, 결정적 usage), alias/privacy 라우팅 함수 | `test_scripted_model.py` |
| 계층형 그래프 | `orchestra/patterns/hierarchical_{graph,tools}.py` | OrgSpec→다단 슈퍼바이저, 팀 내 handoff, report/escalate/request_approval 툴, **handoff 시 팀장 끼어들기 버그 수정** | `test_simulation_golden.py` |
| 서비스 | `orchestra/service.py` | ask/resume, 게이트, 응답 마스킹. LangSmith 는 `config_hook`/`scope` 로 주입 | 〃 |
| 도메인 | `domains/{common,travel,payment}` | hold(TTL)/결제 상태기계/멱등성/금액 한도/hold 만료, 결제 툴은 샌드박스 전용 | `test_payment.py` |
| 시뮬레이션 | `orchestra/simulation.py`, `examples/simulate_hierarchical.py`, `scenarios/*.yaml` 5개 | 실제 그래프 실행, 서사 출력, expect 검증 | `test_simulation_golden.py` |
| NemoClaw 정책 | `orchestra/sandbox/{base,fake}.py`, `sandbox/nemoclaw/policy.py`, `scripts/gen_nemoclaw_presets.py`, `nemoclaw/presets/` | ToolSpec→preset 생성, 금지 규칙 검증, drift 검사, fail-closed 점검 함수, 보안모드↔preset 대응 | `test_policy.py` |

시나리오 5개: `jeju_trip_payment`(승인 결제) · `jeju_payment_rejected`(거절) · `payment_over_limit`(한도 초과 차단) · `pii_bulk_blocked`(게이트) · `sandbox_egress_denied`(egress 거부).

## 2. Codex 가 할 일 (순서대로, 1항목 = 1 PR)

각 항목은 **완료 조건**을 만족해야 한다. 공통: `pytest -q` 전체 통과(기존 60개 수정 금지), `[VERIFY]` 는 설치된 버전에서 확인 후 PR 본문에 기록.

| # | 작업 | 설계서 | 완료 조건 |
|---|---|---|---|
| **C1** | **패키징 M0**: `pyproject.toml`(extras: dev/langsmith/nemoclaw), ruff/mypy 설정, `pytest.ini` 흡수, `.env.example` 에 DESIGN §13 변수 추가 | §4, §13 | `pip install -e .[dev]` 후 `pytest -q`, `ruff check .` |
| **C2** | **하위 호환 shim**: `travel_planner/hierarchical_graph.py::build_hierarchical_app(model)` 이 orchestra 를 호출(조직 `orgs/travel_legacy.yaml` 신규: 기존 3팀 8에이전트 17툴 재현). `travel_planner/observability.py` 의 MASK_RULES/BLOCK_RULES 는 `orchestra.filters.builtin` 것을 import 하도록 **중복 제거**(동작 불변). 기존 `tests/test_observability.py` 14개 그대로 통과 | §2-6, §14 | 기존 14개 + 신규 통과 |
| **C3** | **벤더링 M1**: `scripts/sync_third_party.py`(+`--check`), `third_party/{claude_plugins,multi_agent_module_lib}/`, `LOCK.json`(repo/commit/sha256), NOTICE·LICENSE(Apache-2.0). module-lib 원본 테스트 10개를 `tests/third_party/` 에서 실행 | §7.1, §8 | `sync_third_party.py --check` OK, 10개 통과 |
| **C4** | **frontmatter 파서**: `orchestra/catalog/frontmatter.py` — PyYAML safe_load, 여러 줄 `description: \|`, frontmatter 없는 md(skill-creator agents), 본문 안 예시 frontmatter 무시, `tools:` 문자열/리스트/주석 정규화, `model:` 주석 제거·미지원값→inherit+경고 | §1(6), §7.2 | `tests/test_catalog.py` (케이스 4종은 DESIGN §7.2) |
| **C5** | **카탈로그**: 플러그인 14개 스캔 → `PluginCatalog`, 에이전트 ID `plugin/agent`(code-reviewer 충돌 해소), skills `load_skill` 툴(progressive disclosure, 20000자 상한), commands, hooks.json. `python -m orchestra.catalog list\|show\|validate` | §7.3~7.5 | 14 플러그인·원본 에이전트 16개 로드, 표 출력 |
| **C6** | **툴 매핑**: Claude Code 툴 → 로컬 구현. Read/Grep/Glob/LS 는 `ORCHESTRA_WORKSPACE` 하위로 **경로 고정(심볼릭 링크 탈출 차단)**, Write/Edit/Bash 는 ToolSpec(EXEC, sandbox=required). 미지원 툴은 제거+`dropped_tools` 경고 | §7.3 | 경로 탈출 테스트(`../`, symlink) 통과, sandbox off 면 dry-run diff |
| **C7** | **claude_dev DomainPack + org**: `domains/claude_dev/pack.py`, `orgs/claude_dev.yaml`(feature_lead/review_lead/platform_lead). `review_lead` 6명은 **팬아웃 자동 분할**(이미 구현됨)로 sub-lead 생성 확인 | §7.6 | `build_hierarchical_app("orgs/claude_dev.yaml", ScriptedModelFactory)` 빌드 성공 |
| **C8** | **module-lib 어댑터 5종**: guardrail→`code_guardrail` 필터(+security-guidance `patterns.py` 합집합), loop_control→`loop_node`, review_aggregator→reducer, orchestrator→pipeline 서브그래프, cli_subagent→`ClaudeCliAgent` | §8 | 어댑터별 단위 테스트 + 시나리오 `ralph_loop_cap` |
| **C9** | **hookify 규칙 → 필터**: `.local.md` event(bash/file/prompt/stop/all)→stage 매핑, block/warn, 예제 4개 변환 테스트 | §7.4 | `test_filters.py` 확장 |
| **C10** | **시나리오 추가**: `injection_in_tool_output`(post_tool 가드), `pr_review_parallel`, `ralph_loop_cap`, `concert_swap` + `domains/concert_demo/`(툴 4개, **코어 수정 0**) | §11.5, §12.3 | `test_simulation_golden.py` 가 자동으로 새 시나리오 포함, `test_domain_swap` |
| **C11** | **LangSmith M7**: `orchestra/observability/` 로 기존 observability/monitoring 이관+확장, DESIGN §9.2 의 24개 기능, `showcase` CLI(`--offline` 필수), `OrchestraService(config_hook, scope)` 연결. `payment_safety`·`trajectory_match` 평가자, `scenarios/*.yaml` → 데이터셋 | §9 | `showcase all --offline`, `test_langsmith_offline.py`(로컬 수신 서버로 masked 원문 0건·closed 0바이트) |
| **C12** | **NemoClaw 실행 계층**: [`NEMOCLAW_ROADMAP.md`](NEMOCLAW_ROADMAP.md) 의 N1~N6 | §10 | 로드맵 각 단계의 완료 조건 |
| **C13** | **`simulate_hierarchical.py` 확장 옵션**: `--pattern`, `--org`, `--trace` | §12.1 | 옵션별 동작 |
| **C14** | **playground 탭 / README 도식 갱신 / `langgraph.json`** | §9.4 | 수동 확인 |
| **C15** | **v1 백엔드**: 현재 `create_react_agent` 가 LangGraph 1.x 에서 DeprecationWarning(`langchain.agents.create_agent` 로 이동). `AgentBackend` 추상화 후 v1 구현, parity 테스트 | §6.1 | 같은 시나리오가 두 백엔드에서 같은 툴 순서 |

## 3. 설계서와 달라진 점 (구현이 우선)

| 항목 | 설계서 | 실제 구현 | 이유 |
|---|---|---|---|
| `ToolSpec.egress` | `list[str]` ("host:port") | `list[EgressRule]`(host/port/protocol/rules[method,path]) | preset 에 L7 규칙이 필요 |
| `orchestra/state.py` | OrchestraState 등 | **없음**. `TaskBrief`/`TeamReport` 는 `hierarchical_tools.py` | 하위 그래프 기본 상태로 충분, 재개 안전한 상태는 `Runtime`(승인 장부·이벤트)이 담당 |
| 멱등성 | 필터가 이전 결과 반환 | 필터는 **키 주입만**, 중복 방지는 **장부(provider)** | 필터는 결과를 돌려줄 수 없음 |
| 결제 툴 | quote_total, create_intent(amount) | `create_payment_intent(hold_ids)` — **금액은 서버가 hold 합계로 계산**(LLM 이 금액을 못 정함) | 금액 위조 방지 |
| `confirm_booking` | COMMIT | **RESERVE** | 금전 이동이 없고, 이미 승인된 capture 의 결과라 이중 승인 방지 |
| `approval_required` 필터(payment) | 도메인 필터 | 코어 `side_effect_policy` + 툴 래퍼 마지막 방어선 | 도메인 무관하게 COMMIT 보호 |
| 승인 payload | 1개 | 체인의 **첫** REQUIRE_APPROVAL (amount_limit order 40 < side_effect_policy 50 이라 금액·가맹점 문구가 쓰임) | |
| 시뮬레이션 `--mode` | print=스크립트 직접 호출 / graph=실제 그래프 | **둘 다 실제 그래프**. print=서사, graph=서사+mermaid+검증 | 직접 호출은 검증 가치가 없음 |
| 룸 hold 해제 | 거절 시 hold 해제 | TTL(15분) 자동 만료 | 에이전트 툴 3개 제한. 필요하면 C-후속으로 `release_hold` |
| 병렬 팀(`parallel`/`reducer`) | Send 팬아웃 | **스펙 필드만** 존재, 빌더는 무시 | C8(리뷰 어댑터)에서 구현 |
| `AliasModelFactory.builder` | provider 생성 | 골격만(NotImplementedError) | C-후속(실모델 연결) — 키·모델 ID 필요 |

## 4. 알려진 제약 / 주의 (Codex 가 건드리기 전에 읽을 것)

1. **interrupt 재개 = 툴 노드 재실행.** `tool_wrapper.run` 에서 `hitl.request()` **앞쪽 코드는 부작용이 없어야** 한다(필터·키 계산만). 이벤트는 `dedupe_key` 로 중복 방지. 이 규칙을 깨면 결제가 2번 일어난다 → `test_approval_resume_runs_capture_exactly_once` 가 지킨다.
2. **`langgraph_supervisor` 는 멤버마다 `member→supervisor` 고정 엣지를 건다.** handoff 를 쓰는 멤버는 `_detach_to_supervisor` 로 조건부 엣지로 바꿨다. 이 함수를 지우면 팀장이 handoff 도중 끼어든다 → `test_handoff_does_not_wake_team_supervisor_midway`.
3. **`budget` 필터는 재개 시 카운트가 1~2 늘 수 있다**(보수적으로 설정).
4. **`ApprovalLedger`/`EventLog`/`MockPaymentProvider` 는 프로세스 메모리.** 다중 프로세스·재시작 후 재개는 지원하지 않는다. 운영 전환 시 영속 저장소 필요(후속).
5. `thread_id` 는 전역 변수가 아니라 LangGraph config 에서 읽는다(동시 요청 안전). 새 코드도 `rt.thread_id` 를 쓰지 말 것.
6. 프롬프트 `file:` 경로는 저장소 루트 기준.
7. 실모델·실 LangSmith·실 NemoClaw 경로는 **이 환경에서 검증하지 못했다**(`ScriptedChatModel`/`FakeSandboxRunner` 로만 검증). 해당 코드는 `-m integration|langsmith|nemoclaw` 마커로 분리.
8. DeprecationWarning 180개(create_react_agent)는 C15 에서 해소.

## 5. PR 규칙
AGENTS.md 의 PR 본문 템플릿을 쓴다. 작업 브랜치는 `04-…` 부터 이어서 번호를 붙인다(`03-orchestra-core` 가 이번 코어 구현).
