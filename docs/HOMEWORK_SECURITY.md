# 숙제: NemoClaw 보안 하네스 구현 (참고 저장소에서 쓸만한 것을 골라 개발)

> PM이 방향·리뷰를 맡고, 개발자는 아래 참고 저장소에서 **쓸만한 것을 직접 골라** 이 저장소에 구현해 온다. 결과물은 PR 로 받아 merge 한다.
> 먼저 읽을 것: `README.md` → `docs/DESIGN.md` → `docs/CODEX_HANDOFF.md` → `docs/NEMOCLAW_ROADMAP.md` → `docs/SECURITY_ROADMAP.md` → `AGENTS.md`.

## 0. 목표
이 저장소의 계층형 멀티에이전트 코어 위에 **NemoClaw 실행 계층(N1~N3)** 과 **보안 하네스(S1~S8 중 선택)** 를 구현한다. 도메인(여행·결제)은 예제일 뿐이며, 보안 계층은 도메인과 무관하게 동작해야 한다.

## 1. 참고 저장소와 쓰임새 (개발자가 취사선택)

| 저장소 | 확인된 성격 | 이 프로젝트에서의 후보 용도 |
|---|---|---|
| [NemoClaw](https://github.com/ai-agent-orchestrator/NemoClaw) | NVIDIA NemoClaw 포크. OpenShell 샌드박스에서 에이전트를 더 안전하게 실행하는 참조 스택(alpha). 네트워크 정책·preset·승인 흐름·managed inference 포함. 최상위에 `nemoclaw-blueprint/`, `schemas/`, `docs/`, `SECURITY.md` 등 | **정책 스키마·CLI 실측** → `sandbox/nemoclaw/` 러너와 `policy.py` 를 실제 스키마에 맞춰 보정(N1, N3). `[VERIFY]` 항목 해소 |
| [security-guidance](https://github.com/ai-agent-orchestrator/claude-10-plugin-for-multi-agent/blob/readme-ko/plugins/security-guidance) (Claude 플러그인) | *세션에서 내용을 읽지 못함 — 개발자가 먼저 확인* | Claude 쪽 보안 가드레일. 코드 변경 시점 보안 점검 규칙을 `FilterChain` 필터(`pre_tool`)나 `security_lead` 팀 에이전트로 흡수 |
| [garak](https://github.com/ai-agent-orchestrator/garak) | LLM 취약점 스캐너("nmap for LLMs"). 프롬프트 인젝션·데이터 유출·탈옥 등 probe, detector, generator, JSONL 리포트·hitlog | **레드팀 회귀(S7)**: 에이전트 서비스를 garak generator 로 감싸 probe 실행 → 결과를 LangSmith 데이터셋/CI 게이트로 |
| [AI-agents-for-cybersecurity](https://github.com/ai-agent-orchestrator/AI-agents-for-cybersecurity) | 학습 자료(5파트: 기초→프롬프팅→체이닝→RAG→에이전트). CISA KEV 에이전트, 위협 헌팅/사고 분석 체인, SSRF 예제, MCP 포트스캔 서버 등 | **방어용만** 선별: KEV 기반 취약점 우선순위, 사고 분석 체인 → `security_lead` 팀의 `scan_agent`. 포트스캔/공격 자동화 계열은 **채택 금지**(이중 용도, 승인된 자산 한정) |
| [security-audit-skill](https://github.com/ai-agent-orchestrator/security-audit-skill) | 6단계 감사 스킬(정찰→탐색→검증→구조화 출력→독립 검증→보고). `findings.json`/`coverage-ledger.json` 스키마와 Node 검증기, 샌드박스 필요 | **S1/S2 스캐너 어댑터**의 출력 스키마·검증 파이프라인 모델. `confirmed / needs_validation / rejected` 판정을 CI 게이트와 NemoClaw 샌드박스(코드 실행 검증)에 연결 |

> 위 "확인된 성격"은 각 저장소 README 요약 기준이다. 라이선스·유지 상태·의존성은 **채택 전에 개발자가 직접 확인**하고, 코드를 가져올 때는 라이선스 고지를 `THIRD_PARTY.md` 에 남긴다.

## 2. 과제 구성 (우선순위 순, 7주 가정)

### 필수
1. **R0. 환경 점검 (첫 주 병행)** — NemoClaw 설치, `nemoclaw`/`openshell` CLI 실측. 결과를 `docs/NEMOCLAW_REALITY.md` 에 기록(실제 명령, 정책 스키마, 달라진 점). 계획 문서의 `[VERIFY]` 를 실측값으로 교체.
2. **R1. 실제 러너 (N1)** — `NemoClawRunner(SandboxRunner)`: CLI 래퍼(명령 allowlist, dry-run, 타임아웃, 출력 크기 상한), `doctor`. `FakeSandboxRunner` 와 **동일한 계약 테스트**를 통과.
3. **R2. 드리프트 점검 (N3/NS1)** — 기동 시 적용된 정책과 `gen_nemoclaw_presets.py` 산출물 비교, 불일치면 기동 실패.
4. **R3. 스캐너 인터페이스 (S1)** — `SecurityScanner` 프로토콜 + `FakeScanner`; `findings.json` 스키마(`confirmed/needs_validation/rejected`) 채택. 코어는 특정 제품을 import 하지 않는다(기존 import 금지 테스트 방식으로 강제).
5. **R4. 레드팀 회귀 (S7)** — garak probe 를 최소 3종(예: promptinject, encoding, 데이터 유출 계열) 연결하고, 이 저장소 전용 케이스 5개 이상 추가: 간접 인젝션, handoff 오염, 도구 결과 위장, 금액 조작, PII 유출. 모두 BLOCK/승인요구로 끝나야 통과.

### 선택 (시간 남으면, 하나 이상)
- **O1.** security-guidance 규칙을 필터로 흡수 (`filters/builtin/` 또는 `domains/security/`).
- **O2.** `security_lead` 팀(`scan_agent`/`policy_agent`/`redteam_agent`) — **제안만**, 적용은 COMMIT 등급 승인.
- **O3.** 해시 체인 감사 로그(S8) + 승인 서명.
- **O4.** CI 워크플로(PR 스캔, High 이상 머지 차단).

## 3. 완료 기준 (PR 체크리스트)
- [ ] 기존 테스트 60개 **수정 없이** 통과, 신규 테스트 추가(정상·실패·fail-closed 경로 포함)
- [ ] LLM·네트워크·NemoClaw 없이도 `pytest` 가 돈다(실제 연동 테스트는 `-m nemoclaw` 마커로 분리)
- [ ] 시뮬레이션 시나리오 1개 이상 추가(`scenarios/`) — 보안 차단·승인·샌드박스 거부가 눈에 보이게
- [ ] `scripts/gen_nemoclaw_presets.py --check` 통과
- [ ] 가져온 코드/아이디어 출처와 라이선스를 `THIRD_PARTY.md` 에 기록
- [ ] 공격 도구 채택 금지: 승인된 대상 외 스캔·익스플로잇 코드는 넣지 않는다
- [ ] 비밀키·토큰을 커밋하지 않는다(샌드박스에 장기 키 금지 원칙)
- [ ] `docs/HANDOFF` 갱신: 끝난 것 / 막힌 것 / 다음 단계

## 4. 진행 방식
- 브랜치: `feat/<과제ID>-<요약>`, PR 단위는 과제(R0~R4, O1~O4) 하나씩. PM 리뷰 후 merge.
- 커밋: 공동 작업이면 메시지 끝에 `Co-authored-by: 이름 <이메일>` 추가.
- 주 1회 데모: 시뮬레이션 CLI 로 해당 주 결과를 보여준다(PM 이 코드를 안 읽어도 확인 가능하게).
- 막히면 24시간 내 PM 에게 올린다. 특히 NemoClaw 실측값이 문서와 다를 때는 **문서를 고치고** 설계 이탈 표에 적는다.

## 5. 1주차 학습 과제 (보안 먼저)
1. `docs/SECURITY_ROADMAP.md` 의 위협 8가지를 각각 이 저장소의 어느 코드가 막는지 표로 정리(막지 못하는 것은 "미구현" 표시).
2. garak 를 로컬 모델이나 테스트 generator 로 1회 실행하고 JSONL 리포트·hitlog 읽는 법 정리.
3. security-audit-skill 의 `findings.json` 스키마를 읽고, 이 저장소 `Findings` 모델 초안 작성.
4. NemoClaw `SECURITY.md` 와 네트워크 정책 문서를 읽고 deny-by-default 와 승인 흐름을 한 장으로 요약.
