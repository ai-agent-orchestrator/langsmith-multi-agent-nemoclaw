# Multi-Agent Orchestra — 계층형 슈퍼바이저 × LangSmith × NemoClaw

**한국어** · [English](README.md)

날씨·맛집·숙소·결제 등 여러 전문 에이전트를 **총괄 → 팀장 → 실무자** 계층으로 조직하고,
**도메인(여행·결제)은 교체 가능한 팩**으로, **보안 필터는 5단계 체인**으로 끼워 넣으며,
위험한 실행(결제·코드 실행)은 **NemoClaw/OpenShell 샌드박스**로 격리하는 설계·구현 저장소입니다.
LLM·네트워크·NemoClaw 없이도 **실제 컴파일된 그래프**를 시나리오로 끝까지 실행해 검증할 수 있습니다.

> **현재 상태 (2026-11-22)** — 코어는 구현·테스트 완료(60 tests), 나머지는 Codex 인수인계 중입니다.
> 실모델 · 실제 LangSmith 서버 · **실제 NemoClaw 연동은 이 환경에서 검증하지 못했습니다** (아래 [NemoClaw 보완 예정](#nemoclaw-보완-예정) 참고).

| 문서 | 내용 |
|---|---|
| [`docs/DESIGN.md`](docs/DESIGN.md) | 전체 설계서 (아키텍처·스펙·LangSmith 24개 기능 지도·NemoClaw 배치) |
| [`docs/CODEX_HANDOFF.md`](docs/CODEX_HANDOFF.md) | **인수인계**: 끝난 것 / 남은 일 C1~C15 / 설계 이탈 / 주의사항 |
| [`docs/NEMOCLAW_ROADMAP.md`](docs/NEMOCLAW_ROADMAP.md) | NemoClaw 보완 계획 N1~N7, 한계, 설치 후 확인 체크리스트 |
| [`docs/SECURITY_ROADMAP.md`](docs/SECURITY_ROADMAP.md) | **멀티에이전트 시대 보안 설계**, NemoClaw 강화, Claude Security 연동 계획 |
| [`AGENTS.md`](AGENTS.md) | Codex 작업 규칙·PR 템플릿 |

## 빠른 실행

```bash
pip install -r requirements.txt
python -m pytest -q tests                                   # 60 passed
python -I examples/simulate_hierarchical.py --list          # 시나리오 5개
python -I examples/simulate_hierarchical.py                 # 제주 + 호텔 hold + 승인 결제
python -I examples/simulate_hierarchical.py --scenario jeju_payment_rejected
python -I examples/simulate_hierarchical.py --scenario sandbox_egress_denied --mode graph
python scripts/gen_nemoclaw_presets.py --check              # NemoClaw egress preset drift 검사
```

## 이번에 구현된 것

```
orchestra/                    도메인을 모르는 코어
  specs.py  org.py            선언형 조직도(OrgSpec), 구조 검증, 팬아웃 자동 분할(팀장당 선택지 ≤5)
  filters/                    FilterChain(5단계) + 내장 필터 + 툴 래퍼(승인/샌드박스/마지막 방어선)
  patterns/                   hierarchical_graph.py, hierarchical_tools.py  ← 계층형 + handoff + report/escalate
  models/                     ScriptedChatModel(LLM 없이 실제 그래프 실행), ModelFactory, 프라이버시 라우팅
  sandbox/                    SandboxRunner 계약, Fake 러너, nemoclaw/policy.py (egress preset 생성·검증)
  hitl.py runtime.py service.py simulation.py
domains/                      교체 가능한 도메인 팩
  travel/                     툴 17개, 예약은 hold(임시 점유)까지만
  payment/                    intent → 사용자 승인 → capture → 예약 확정, 상태기계·멱등성·한도·hold 만료
orgs/travel_payment.yaml      조직도 (총괄 1 + 팀장 4 + 실무자 10)
scenarios/*.yaml              시뮬레이션·검증 공용 시나리오 5개
nemoclaw/presets/*.yaml       ToolSpec 에서 생성된 egress 정책 (CI drift 검사)
```

### 핵심 설계 결정
- **도메인 교체**: 툴·에이전트·필터·시나리오는 `domains/*` 팩. 코어는 도메인을 import 하지 않는다(테스트로 강제).
- **금액은 LLM 이 정하지 않는다**: `create_payment_intent(hold_ids)` 가 hold 합계로 서버에서 계산.
- **승인은 겹으로**: ① 금액 필터 → ② `side_effect_policy`(COMMIT=승인 필요) → ③ **툴 래퍼 마지막 방어선**(필터를 다 꺼도 승인 장부 없이는 capture 불가). 승인은 1회용, 무응답=거절.
- **interrupt 재개 안전**: 재개 시 툴 노드가 재실행돼도 결제는 정확히 1번(테스트 고정).
- **필터는 fail-closed**: 필터가 예외를 던지면 BLOCK. 샌드박스 모드인데 러너가 없으면 실행 중단.
- **계층형 버그 수정**: `langgraph_supervisor` 가 멤버마다 거는 `member→supervisor` 고정 엣지 때문에, handoff 중 팀장이 끼어드는 문제를 발견·수정(`_detach_to_supervisor`).
- **NemoClaw egress 정책은 선언에서 생성**: 같은 host:port 가 두 샌드박스에 열리면 검증 실패, 추론 제공자 호스트·`access: full`·와일드카드 금지, `closed` 모드는 LangSmith preset 자체를 만들지 않음.

### 실행 결과 (`python -I examples/simulate_hierarchical.py`, 결제 구간 발췌)
LLM 없이 라우팅만 스크립트로 고정하고, **실제 그래프·필터·승인·샌드박스 코드 경로**를 모두 탄다. 값은 mock.

```text
  [LLM #19] payment_lead: checkout_agent 호출 → 호출 transfer_to_checkout_agent
⑫ checkout_agent (실무자)
  [LLM #20] checkout_agent: 호출 create_payment_intent(hold_ids=['hold_0001'])
      [FILTER] pre_tool idempotency(create_payment_intent): modify — idempotency_key injected
      [FILTER] pre_tool side_effect_policy(create_payment_intent): route_sandbox — create_payment_intent runs in orchestra-payments
      [SANDBOX orchestra-payments] create_payment_intent ok (policy rev 1)
      · tool create_payment_intent(hold_ids=['hold_0001']) -> {'intent_id': 'pi_0001', 'amount': 180000, 'currency': 'KRW', 'status': 'created', 'hold_ids': ['hold_0001']}
  [LLM #21] checkout_agent: 호출 capture_payment(intent_id='pi_0001')
      [FILTER] pre_tool idempotency(capture_payment): modify — idempotency_key injected
      [FILTER] pre_tool amount_limit(capture_payment): require_approval — 결제 180,000원 사용자 승인 필요
      [FILTER] pre_tool side_effect_policy(capture_payment): require_approval — commit tool capture_payment needs user approval
      [HITL] 사용자 승인: 예
      [FILTER] pre_tool side_effect_policy(capture_payment): route_sandbox — capture_payment runs in orchestra-payments
      [SANDBOX orchestra-payments] capture_payment ok (policy rev 1)
      · tool capture_payment(intent_id='pi_0001') -> {'intent_id': 'pi_0001', 'status': 'captured', 'receipt_id': 'rcpt_0001', 'amount': 180000, 'currency': 'KRW'}
  [LLM #22] checkout_agent: 호출 confirm_booking(intent_id='pi_0001')
      [FILTER] pre_tool idempotency(confirm_booking): modify — idempotency_key injected
      [FILTER] pre_tool side_effect_policy(confirm_booking): route_sandbox — confirm_booking runs in orchestra-payments
      [SANDBOX orchestra-payments] confirm_booking ok (policy rev 1)
      · tool confirm_booking(intent_id='pi_0001') -> {'intent_id': 'pi_0001', 'status': 'confirmed', 'receipt_id': 'rcpt_0001', 'bookings': ['h1 standard 2026-11-28 1박 2명']}
  [LLM #23] checkout_agent: 결제 180,000원 완료, 영수증 rcpt_0001, 예약 확정
⑬ payment_lead (총괄/팀장)
  [LLM #24] payment_lead: 호출 report_to_parent(status='done', summary='결제 완료 및 예약 확정', artifacts={'receipt_id': 'rcpt_0001'})
      [REPORT payment_lead] done: 결제 완료 및 예약 확정
  [LLM #25] payment_lead: 결제·확정 완료 → 총괄에 보고
⑭ travel_director (총괄/팀장)
  [LLM #26] travel_director: 최종: 비(70%) → 실내 코스(고기국수 → 흑돼지), 제주시내 호텔 1박 hold → 180,000원 승인·결제·예약 확정 완료.

[interrupt #1] 180,000원을 Orchestra Travel (mock)에 결제할까요? (결제 후 환불 가능)  → 응답: 예

최종 답변:
  최종: 비(70%) → 실내 코스(고기국수 → 흑돼지), 제주시내 호텔 1박 hold → 180,000원 승인·결제·예약 확정 완료.

슈퍼바이저(총괄/팀장) LLM 호출 13회, 전체 LLM 호출 26회 (조직 깊이 2)

--- expect 검증 ---
PASS
```

전체 흐름: `travel_director → planning_lead → (weather_agent ⇢ restaurant_agent) → booking_lead → hotel_agent → payment_lead → checkout_agent`.

| 시나리오 | 검증 포인트 |
|---|---|
| `jeju_trip_payment` | 전체 계층 흐름 + 팀 내 handoff + HITL 승인 + 샌드박스 결제 + 예약 확정 |
| `jeju_payment_rejected` | 사용자가 거절 → capture 미실행, 확정 없음 |
| `payment_over_limit` | 스위트 2,400,000원 → `amount_limit` 차단, 승인 요청조차 없음 → 대안 제시 |
| `pii_bulk_blocked` | 게이트에서 차단, LLM 호출 0회 |
| `sandbox_egress_denied` | 결제 호스트가 정책에서 빠짐 → 샌드박스가 egress 거부 → 결제 미실행, 운영자 승인 안내 |

## 분담과 남은 작업

틀리기 쉬운 코어는 구현·테스트까지 끝났고, 규칙이 정해진 나머지는 Codex 가 이어받는다. 상세 표는 [`CODEX_HANDOFF.md`](docs/CODEX_HANDOFF.md).

| 영역 | 상태 |
|---|---|
| 스펙/조직도/팬아웃, 필터 체인·내장 필터, 툴 래퍼, HITL, ScriptedChatModel | ✅ 완료 |
| 계층형 그래프·도구, 서비스, travel·payment 팩, 시뮬레이션·시나리오 5종 | ✅ 완료 |
| NemoClaw 정책 생성·검증·drift·fail-closed 함수, Fake 샌드박스 | ✅ 완료 (실제 NemoClaw 연동 ⬜) |
| 패키징(pyproject), 기존 구조 shim·중복 제거 | ⬜ C1~C2 |
| 플러그인 14개 벤더링·카탈로그·툴 매핑·`claude_dev` 팩 | ⬜ C3~C7 |
| module-lib 어댑터 5종, hookify 규칙 필터, 시나리오 4종 추가·`concert_demo` | ⬜ C8~C10 |
| **LangSmith 전 기능**(24개) + `showcase` CLI | ⬜ C11 |
| **NemoClaw 실행 계층** (러너·워커·추론·감사·코드 샌드박스) | ⬜ C12 → 아래 |
| v1 백엔드(`langchain.agents.create_agent`), 시나리오 parity | ✅ C15 (스크립트 모델 검증) |
| playground 탭 | ⬜ C14 |

## NemoClaw 보완 예정

현재는 **"정책을 선언에서 생성하고 검증하는 계층"까지**이며, 실제 샌드박스에서 실행하는 계층은 아직 없다. 계획 전문은 [`docs/NEMOCLAW_ROADMAP.md`](docs/NEMOCLAW_ROADMAP.md).

| 단계 | 내용 | 상태 |
|---|---|---|
| N0 | ToolSpec→preset 생성·검증·drift, Fake 러너, 보안모드↔preset 대응 | ✅ |
| N1 | 실제 러너: `nemoclaw`/`openshell` CLI 래퍼(allowlist·dry-run), `doctor` | ⬜ |
| N2 | 샌드박스 내부 워커 + 전송(stdio/포워딩), **이중 필터 검사**, 분산 트레이싱 헤더 | ⬜ |
| N3 | 기동 시 적용 preset 일치 점검(**불일치면 기동 실패**), LangSmith 경로 규칙 실측 | ⬜ |
| N4 | 추론은 `inference.local` 경유, 프라이버시 라우터(PII/PCI → 로컬 모델) | ⬜ |
| N5 | egress deny/승인 이벤트 → LangSmith feedback·annotation queue | ⬜ |
| N6 | 코드 실행 샌드박스(`orchestra-code`), Landlock/프로세스 계층 정의, (선택) Deep Agents Code | ⬜ |
| N7 | `Dockerfile.worker`, 운영 절차 문서 | ⬜ |

**알려진 한계**: ① `binaries` 가 `python3*` 글롭이라 실행파일 단위가 아니라 **샌드박스 단위 격리**에 의존 → 결제 샌드박스엔 워커 외 코드를 두지 않음, ② `openshell policy set` 은 정책 전체를 교체하므로 운영 변경은 `policy-add` 로만, ③ 파일시스템·프로세스 계층은 **샌드박스 재생성**이 필요, ④ 승인·결제 장부는 현재 프로세스 메모리, ⑤ CLI·정책 스키마는 NemoClaw 버전마다 바뀔 수 있어 **설치된 버전에서 먼저 확인**(`[VERIFY]`).

## 향후 계획 — 보안 최상 지향 (NemoClaw 강화 + Claude Security 연동)

멀티에이전트는 에이전트 간 handoff·툴 결과·플러그인·메모리마다 신뢰 경계가 생긴다. 그래서 "프롬프트를 잘 쓰자"가 아니라 **Zero-Trust Agent Mesh** 로 설계한다. 전문은 [`docs/SECURITY_ROADMAP.md`](docs/SECURITY_ROADMAP.md).

- **원칙**: LLM 은 정책 결정자가 아님(결정적 필터가 결정) · 에이전트 간 메시지는 데이터일 뿐 지시가 아님 · 권한은 위임 시 줄어들기만 함 · 승인은 `(tool, 인자 해시, 금액, 만료)` 에 서명으로 바인딩 · 실패는 닫는다 · 모든 차단·거부는 LangSmith 로 관측.
- **NemoClaw 강화(NS1~NS9)**: 정책 드리프트 상시 점검, 위험도별 샌드박스 티어(read / payments / code), 샌드박스 내부 이중 필터, `inference.local` + 프라이버시 라우팅, egress 거부 → 자동 격리·리뷰, `policy-add` 만 허용하는 변경 승인, 시크릿 위생, 리소스 상한, 이미지 핀 고정·SBOM.
- **Claude Security 연동(S1~S8)**: ① PR 마다 CI 스캔(High 이상 머지 차단·패치안 코멘트) ② 플러그인 14개 **인입 게이트**(SHA 핀 + 해시 + 스캔 통과분만 등록) ③ `security_lead` 팀(scan / policy / redteam 에이전트, **제안만 하고 적용은 사람 승인**) ④ 레드팀 회귀 스위트(인젝션·handoff 오염·금액 조작·PII 유출) ⑤ 해시 체인 감사 로그.
- **현황**: 설계 단계. Claude Security·NemoClaw 의 실제 인터페이스는 확인 전(`[VERIFY]`)이라 코어는 **인터페이스 중립 어댑터**로 붙인다.

---

# 부록 — 기존 골격 (supervisor / handoff / hybrid / hierarchical, LangSmith 연동)

> 아래는 이 저장소의 출발점이던 설계 골격(`travel_planner/`)의 설명입니다. 코드는 그대로 남아 있으며(기존 테스트 14개 통과),
> 신규 `orchestra/` 로의 하위 호환 shim 은 Codex 작업 C2 입니다. 신규 계층형 구현과 시뮬레이션은 위 섹션을 따르세요.

## 구조

- **A**: `supervisor ⇄ {weather, restaurant, hotel}`. 비 오면 실내 맛집 → 맛집 동선 근처 숙소 → 종합. 숙소 에이전트는 예약 툴 없음.
- **B**: `weather ⇄ restaurant ⇄ hotel`. `transfer_to_*` 툴로 제어권을 넘기며 `MemorySaver`로 멀티턴 유지.
- **A+B**: `planner(A) → approval(interrupt) → hotel_booking`. 거절/변경 시 `planner`로 복귀. `draft_plan`을 State로 넘겨 날짜·지역을 다시 묻지 않음.

## 계층형(Hierarchical) 구조

하위 에이전트가 많아져 단일 슈퍼바이저가 감당하기 어려울 때, 중간 관리자(팀장) 슈퍼바이저를 두고 그 위에 총괄 슈퍼바이저를 배치하는 피라미드식 구조입니다.
도메인(팀) 단위로 나눠 각 관리자가 보는 선택지(프롬프트 길이)를 3~5개 이하로 제한하는 것이 핵심입니다.

### 설계 로직 flowchart

![계층형 설계 로직](docs/diagrams/hierarchical-design.png)

- 총괄(Travel Director)은 팀장 3명만 보고, 각 팀장은 자기 팀원만 봅니다.
- 에이전트당 툴은 3개 이하입니다: 날씨 2 / 맛집 3 / 관광지 2 / 항공 3 / 숙소 3 / 렌터카 2 / 번역 1 / 긴급연락 1.

### 작동 흐름 예시 (설계 로직에 요청을 대입한 flowchart)

요청: **"이번 주말 제주도 맛집 투어 짜주고 호텔도 예약해 줘"**

![요청 대입 흐름](docs/diagrams/hierarchical-example.png)

1. **총괄**: 일정 기획과 예약/결제 두 팀이 필요하다고 판단해 `planning_lead`를 먼저 호출합니다.
2. **일정 기획 팀장**: 팀원 `weather_agent`, `restaurant_agent`만 호출해 코스 초안을 만들고 총괄에게 보고합니다. (강수확률 70%이므로 실내 맛집만 선택)
3. **총괄**: 코스 완성을 확인하고 `booking_lead`에게 숙소 예약을 지시합니다.
4. **예약/결제 팀장**: `hotel_agent`가 검색 → 재고 확인 → 예약을 실행하고, 결과를 보고받습니다.
5. **최종 완료**: 총괄이 두 팀장의 보고를 합쳐 사용자에게 답변합니다.

### 계층형 구조 도입 시 주의할 점

- **응답 지연(Latency)과 비용 증가**: `총괄 → 팀장 → 실무자 → 팀장 → 총괄` 순으로 LLM 호출 단계가 깊어지므로 응답이 느려지고 토큰 소모가 큽니다.
- **실무 팁**: 처음부터 계층형으로 짜지 말고 단일 슈퍼바이저나 핸드오프로 시작하세요. 에이전트 개수가 7~8개를 넘어가며 정확도가 떨어질 때 팀을 나누어 계층형으로 승격시키는 것이 좋습니다.

## LangSmith 연동 (`travel_planner/observability.py` 등, 기존 4구조용 외곽 래퍼)

기존 4개 구조(supervisor / handoff / hybrid / hierarchical)는 수정하지 않고, 컴파일된 그래프를 **바깥에서 감싸는** 방식으로 연결합니다.

![LangSmith 연동 흐름](docs/diagrams/langsmith-integration.png)

| 파일 | 역할 |
|---|---|
| `travel_planner/observability.py` | 보안 모드(off/masked/closed), 민감정보 마스킹(anonymizer), `run_config`(태그·메타데이터·`recursion_limit`), 입력 보안 게이트 |
| `travel_planner/service.py` | `TravelService(pattern, model).ask(...)`: 4개 구조를 같은 인터페이스로 실행 (게이트 + 트레이싱 + 스텝 타임아웃) |
| `travel_planner/monitoring.py` | 노드별 토큰·P50/P95/P99·에러율 집계와 튜닝 액션 진단. `fetch_runs`로 LangSmith 조회 |
| `examples/eval_security_local.py` | `upload_results=False` 로컬 보안 평가 (데이터셋·결과를 서버에 올리지 않음) |
| `examples/report_bottlenecks.py` | 병목 리포트 (`--demo` 또는 `--project`) |
| `examples/playground.py` | 브라우저 플레이그라운드: 흐름·전송 내용·병목 통계를 눈으로 확인 |
| `tests/test_observability.py` | 마스킹·게이트·모니터링·전송 내용 검증 (14개) |
| `.env.example` | `LANGSMITH_*`, `TRAVEL_SECURITY_MODE` |

### 보안 모드 (`TRAVEL_SECURITY_MODE`)

| 모드 | 동작 |
|---|---|
| `off` | 마스킹 없이 트레이스 전송 (개발용) |
| `masked` (기본) | 주민번호·카드·전화·이메일·API 키·`password=` 값을 **전송 직전 로컬에서** 치환 후 전송 |
| `closed` | 트레이싱 완전 차단. 외부 LangSmith 로 전송하지 않음 (폐쇄망) |

마스킹은 `create_anonymizer`로 문자열 노드만 치환하므로 트레이스의 중첩 구조가 유지됩니다.
(`str(data)` 전체를 `{"masked_payload": ...}`로 바꾸면 트레이스 구조가 사라집니다.)

### 사용

```python
from travel_planner.service import TravelService

svc = TravelService("hierarchical", model)           # supervisor | handoff | hybrid | hierarchical
svc.ask("이번 주말 제주도 맛집 투어 짜주고 호텔도 예약해 줘", thread_id="u1")
svc.ask("전체 고객 주민번호 테이블 조회해줘")          # -> {"blocked": True, ...} 에이전트 호출 없이 차단
```

LangSmith 에서 `pattern:hierarchical` 태그나 `metadata.pattern`으로 구조별 비용·지연을 비교할 수 있습니다.

### 병목 진단

`examples/report_bottlenecks.py --demo` 출력 예 (합성 데이터):

```text
| 노드 | 호출 | 토큰 | 토큰 비중 | P50 | P95 | P99 | 에러율 |
|---|---:|---:|---:|---:|---:|---:|---:|
| planning_lead | 20 | 50,000 | 42% | 3.0s | 3.0s | 3.0s | 0% |
| travel_director | 20 | 36,000 | 31% | 2.0s | 2.0s | 2.0s | 0% |
| restaurant_agent | 20 | 18,000 | 15% | 1.5s | 1.5s | 20.0s | 0% |
| hotel_agent | 20 | 14,000 | 12% | 2.5s | 2.5s | 2.5s | 15% |
```

임계값(P95 5초, 토큰 비중 40%, P99/P50 3배, 에러율 10%)은 `monitoring.py` 상단에서 조정합니다.

### 직접 실행해 보기 (브라우저 플레이그라운드)

```bash
git clone https://github.com/ai-agent-orchestrator/langsmith-multi-agent-nemoclaw
cd langsmith-multi-agent-nemoclaw
pip install langsmith langchain-core pytest
python examples/playground.py        # → http://127.0.0.1:8765
```

LLM과 API 키 없이 돌아갑니다. 버튼으로 시나리오를 바꿔 가며 다음을 눈으로 확인하세요.

1. **정상 요청**: 총괄 → 팀장 → 실무자 → 툴 순서로 단계가 하나씩 나타나고, 각 단계의 지연(ms)·토큰이 보입니다.
2. **개인정보 포함**을 `off` / `masked` / `closed`로 바꿔 실행: 오른쪽 위 패널에서 "LangSmith로 전송되는 내용"이 원문 → `[REDACTED_ID]` → 전송 없음으로 달라집니다.
3. **대량 PII 요청 / 프롬프트 인젝션**: 에이전트 단계 없이 게이트에서 바로 차단됩니다.
4. **20회 반복**: 아래 병목 통계에 노드별 P95/P99·토큰 비중·에러율과 튜닝 포인트가 쌓입니다.

게이트·마스킹·병목 진단은 실제 모듈 코드이고, 에이전트 호출·지연·토큰은 시뮬레이션입니다.
실제 LLM으로 바꾸려면 `TravelService(pattern, model)`을 사용하세요.

### 검증 결과와 한계

- 로컬 평가 5/5 통과, 테스트 14개 통과 (LLM·외부 네트워크 불필요).
- 전송 내용 검증: 로컬 수신 서버로 `masked`는 원문 주민번호 0건 + `[REDACTED_ID]` 확인, `closed`는 전송 0바이트, `off`는 원문 전송됨(테스트 민감도 확인).
- `closed`에서는 `enforce_closed_network()`가 LangSmith 엔드포인트를 로컬 불통 주소로 돌려, `evaluate()`가 내부에서 만드는 기본 Client 도 외부로 `/info` 조회조차 못 하게 합니다.
- `upload_results`는 최신 langsmith 에서 `upload_runs`의 별칭입니다 (둘 다 동작, beta 경고 출력).
- 실제 LangSmith 서버 연동과 실제 LLM 호출 경로는 이 환경에서 확인하지 못했습니다.

## 사용 예

```python
from langchain_anthropic import ChatAnthropic
from travel_planner import build_hybrid_app

app = build_hybrid_app(ChatAnthropic(model="claude-sonnet-4-5"))
cfg = {"configurable": {"thread_id": "t1"}}
app.invoke({"messages": [("user", "이번 주말 제주도 2박 3일 코스 짜줘")]}, cfg)
# interrupt 발생 시: app.invoke(Command(resume=True), cfg)
```

## 참고

- `book_hotel`은 실서비스에서 결제 직전 `interrupt()`로 최종 확인을 받을 것.
- 모델 ID와 LangGraph 버전별 API(`langgraph-supervisor`, `Command`)는 환경에 맞게 확인 필요.

### 시뮬레이션 CLI (C13)

```bash
python -I examples/simulate_hierarchical.py --org orgs/travel_payment.yaml --pattern tools --trace --json out/sim/trace.json
```

`--pattern graph`가 기본이며 기존 동작을 유지합니다. `tools`는 계층 운영용 툴
팩토리 모듈의 얇은 진입점으로 같은 공통 그래프 빌더를 호출합니다. 별도 오케스트레이션이나
모델 백엔드가 아닙니다. `--org`는 시나리오 원본을 바꾸지 않고 조직 YAML만 대체합니다.
없는 경로나 디렉터리는 그래프 실행 전 종료코드 2와 한 줄 오류로 거절합니다.
기존 `--mode print|graph`는 표시 방식이며 둘 다 실제 그래프를 실행합니다.

`--trace`는 필터 결정(ALLOW 포함), 승인 요청/결과, 샌드박스 라우팅/결과와 차단을
시간순 `seq`로 수집합니다. `[TRACE]` 한 줄마다 JSON 레코드 하나가 나오고 필드 안의
줄바꿈은 이스케이프합니다. `--json` 병용 시 같은 `trace` 배열을 추가합니다.
기존 이벤트 순서·중복 제거를 사용하며, trace 모드 콘솔과 JSON 전체는 보안 모드가
`off`여도 마스킹 경로를 거칩니다. 트레이스 레코드는 진단 필드만 허용하고 승인 바인딩과
전체 툴 인자는 제외합니다. LangSmith 전송을 켜지 않습니다. `--trace`를 생략하면
기존 출력·JSON 구조·이벤트 수집 동작을 유지합니다.

### 장부 영속화 (C22)

기본 `LedgerStore`는 격리된 프로세스 메모리이며 키나 파일이 필요 없습니다.
SQLite는 기존 디렉터리의 명시적 경로를 `ORCHESTRA_LEDGER=sqlite:///C:/private/orchestra/ledger.db`로
지정하고, 환경변수 `ORCHESTRA_LEDGER_HMAC_KEY`에 무작위 비밀값(UTF-8 32바이트 이상)을
제공해야 합니다. 키 누락/길이 부족이면 DB를 열기 전에 기동을 거부합니다.
기본 경로·기본 비밀값은 없습니다. `ORCHESTRA_LEDGER_KEY_ID`는 선택적 공개 교체 라벨이며
생략하면 HMAC에서 식별자를 유도합니다. 스키마 버전은 1이며 알 수 없는 미래 버전은 거부합니다.

원본 C17 ApprovalRecord와 인자는 메모리에만 유지합니다. DB에는 전체 원본 튜플의
HMAC-SHA256, 별도 사용자 HMAC, 키 ID, 만료, 소비 상태와 마스킹된 표시 정보만 저장합니다.
표시는 인자·사용자·요청 원문을 제외하고 금액은 표시할 수 있습니다. 실행 시 원래 만료/요청 ID를
포함한 레코드로 HMAC을 다시 계산합니다. 만료된 승인은 무효이며 `cleanup_expired(now)`로 정리합니다.

승인·이벤트·mock 결제 장부가 같은 저장소를 사용합니다. `BEGIN IMMEDIATE`와 조건부 UPDATE로
동시 소비 1건만 허용하고, 승인 소비·mock 결제 상태·HMAC 멱등 기록을 함께 커밋합니다.
결제 상태 저장 실패 시 함께 롤백합니다. 영속 이벤트는 원문 인자·일반 승인 해시·바인딩 본문을
제외하며 결제 안전성 평가자는 호스트가 생성한 HMAC 참조를 대응시킵니다.
`EventLog(store=store, scope="app")`는 같은 scope의 마스킹된 이벤트와 중복 제거를 복구합니다.
독립 실행에는 다른 scope를 사용하세요. 기본 runtime scope는 `runtime`입니다.

키 교체 시 구 키 작성자를 중지하고 대기 승인을 만료/거절한 뒤 환경 키와 키 ID를 함께 배포합니다.
신 키 인스턴스는 구 키 승인을 소비/덮어쓸 수 없으며 구 메타데이터는 만료까지 읽기 전용으로 유지합니다.
만료 후 정리할 수 있습니다. 멱등 인덱스도 키에 종속되므로 키 교체 전후로 과거 요청을 무조건
재전송하지 않습니다. 기록된 영수증·대기 요청을 확인하고 조정한 뒤 새 작업을 엽니다.
자동 재키잉·원문 복구는 구현하지 않았으며 비밀키를 DB/버전 관리에 저장하지 않습니다.

append_event에는 기본 no-op인 마스킹된 서명/체인 훅 한 지점만 있습니다. O3 서명·해시 체인은
구현하지 않았습니다. 원자성은 로컬 mock/Fake 경로의 범위이며 SQLite COMMIT은 같은 트랜잭션에
참여하지 않는 결제 제공자·원격 샌드박스를 거부합니다. 그래프 체크포인트·hold·모델 큐는 영속화하지
않습니다. 전체 워크플로의 재시작 resume나 실제 PG/NemoClaw 원자성을 검증한 것은 아닙니다.
메모리 모드는 실행 실패 시에도 승인을 소비하는 C17 동작을 유지하며, 트랜잭션에 참여하는 SQLite mock 작업만 승인과 결제 상태를 함께 롤백합니다.
