# 개발 보안 개념 정리 — Multi-Agent × NemoClaw

> 개발자가 코드를 짜기 전에 머릿속에 있어야 할 **개념 지도**. 각 개념은 ① 한 줄 정의 ② 왜 필요한가 ③ 이 저장소의 구현/테스트 위치 순서로 적었다.
> 처음이면 §1~3 → §6(체크리스트) 순서로, 구현 중이면 §4·5·7 을 참조.
> NemoClaw 사실관계는 [`NEMOCLAW_REALITY.md`](NEMOCLAW_REALITY.md)(문서·스키마·소스로 확인) 기준이다.

## 1. 큰 그림: 세 가지 사실에서 출발한다

1. **모델은 속는다.** 입력(웹, 문서, 툴 결과, 다른 에이전트의 메시지)이 곧 지시처럼 읽힌다. → 모델의 판단을 방어선으로 쓰지 않는다.
2. **에이전트는 행동한다.** 코드 실행·네트워크·결제가 같은 루프에 있다. → 행동의 **피해 반경**을 구조로 줄인다.
3. **에이전트는 여럿이다.** 신뢰 경계가 에이전트 사이에도 생긴다. → 내부 메시지도 외부 입력처럼 다룬다.

```
         신뢰할 수 없는 영역                          신뢰하는 영역(결정적 코드)
  ┌──────────────────────────────┐        ┌────────────────────────────────────────────┐
  │ 사용자 입력 · 웹/문서 · 툴 결과 │        │ FilterChain · 승인 장부 · 상태기계 · 정책 검증 │
  │ 다른 에이전트의 handoff 메시지  │ ─────▶ │  (LLM 이 아니라 코드가 허용/차단을 결정)        │
  │ LLM 출력                       │        └───────────────┬────────────────────────────┘
  └──────────────────────────────┘                        ▼
                                              ┌────────────────────────┐
                                              │ 샌드박스(NemoClaw/OpenShell) │ ← 피해 반경 제한
                                              └────────────────────────┘
```

## 2. 핵심 개념 (멀티에이전트 공통)

| 개념 | 한 줄 정의 | 왜 필요한가 | 이 저장소 |
|---|---|---|---|
| **신뢰 경계** | 검증 없이 믿으면 안 되는 선 | 경계마다 검증/격리 지점을 둔다 | FilterChain 5단계(`orchestra/filters/chain.py`) |
| **프롬프트 인젝션(직접/간접)** | 입력에 숨은 지시로 모델 행동 탈취 | 간접형은 툴 결과·웹 문서로 들어옴 | `request_gate`, 레드팀 `tests/test_redteam.py` |
| **최소 권한** | 필요한 도구·네트워크·파일만 | 탈취돼도 할 수 있는 일이 작음 | 에이전트당 도구 ≤3(`AgentSpec`), 팀별 샌드박스 |
| **Confused deputy** | 약한 쪽이 강한 쪽을 속여 대리 수행 | handoff 로 결제 에이전트를 부림 | 금액은 서버 계산, COMMIT 은 항상 승인 |
| **부작용 등급** | READ / RESERVE / COMMIT / EXEC | 위험도별로 통제 강도를 다르게 | `SideEffect`, `side_effect_policy` |
| **HITL(사람 승인)** | 되돌릴 수 없는 행동 전 사람 확인 | 모델 실수의 마지막 안전망 | `hitl.py`, 승인은 1회용·무응답=거절 |
| **마지막 방어선** | 필터가 전부 꺼져도 COMMIT 불가 | 설정 실수·필터 버그 대비 | `filters/tool_wrapper.py` + 골든 테스트 |
| **멱등성** | 같은 요청을 재실행해도 한 번만 효과 | interrupt 재개·재시도에서 이중 결제 방지 | 멱등키 필터, `ApprovalLedger` |
| **fail-closed** | 판단 불가면 차단 | 오류가 우회 경로가 되지 않게 | 필터 예외=BLOCK, 샌드박스 부재=중단, 드리프트=기동 실패 |
| **이중 방어** | 서로 독립인 여러 겹 | 한 겹 버그가 전체 붕괴가 되지 않게 | 호스트 필터 + 샌드박스 정책 + 코드 가드 |
| **불변식(invariant)** | 어떤 경로로도 깨지면 안 되는 규칙 | "테스트 통과"가 아니라 "속성 유지"를 검증 | `orchestra/security/invariants.py` 4종 |
| **공급망** | 플러그인/스킬/MCP/의존성의 신뢰 | 악성 도구가 곧 권한 | SHA 고정·해시·스캔 인입 게이트(`security/intake.py`, `scripts/intake_gate.py`), `THIRD_PARTY.md` |
| **감사 가능성** | 누가 무엇을 왜 승인했는지 사후 증명 | 사고 분석·부인 방지 | 해시 체인 로그(`security/audit.py`) |
| **최소 정보** | 로그·트레이스에 원문 PII 남기지 않음 | 관측 도구가 유출 경로가 되지 않게 | `pii_mask`, 보안 모드 off/masked/closed, 감사 로그 해시화 |

### 2.1 멀티에이전트에서만 생기는 문제
- **Handoff 오염**: 하위 팀 보고서의 값(예: amount)을 상위가 그대로 신뢰하면 위조가 전파된다 → 값은 **원천(서버 계산)** 에서 다시 구한다. (`redteam_handoff_amount_poison` 시나리오)
- **권한 합성**: 약한 도구 A 의 출력이 강한 도구 B 의 입력이 되면 A 의 신뢰 수준이 B 로 승격된다 → 출처 라벨(taint)과 인자 검증이 필요(로드맵 S2).
- **무한 handoff·비용 폭주**: 에이전트끼리 핑퐁 → `recursion_limit`, `budget` 필터. (폭주 회귀 테스트는 아직 없음)
- **프레임워크 함정**: 라이브러리가 만든 암묵적 엣지가 의도와 다른 실행 순서를 만든다(실제로 발견·수정: `_detach_to_supervisor`). → **그래프 동작도 테스트로 고정**한다.

## 3. NemoClaw / OpenShell 개념 (실행 격리 계층)

| 개념 | 설명 | 개발자가 지킬 것 |
|---|---|---|
| **샌드박스** | 에이전트 실행을 격리하는 단위(OpenShell 위에서 NemoClaw 가 구성) | 위험 도구는 반드시 샌드박스에서. 이름 규칙 1~19자·소문자·단일 하이픈 |
| **다섯 보호 계층** | network / filesystem / process / gateway auth / inference, 모두 deny-by-default | 파일·프로세스는 생성 시 고정(바꾸려면 재생성) → **처음 설계가 중요**. 네트워크·추론은 운영 중 변경 가능 |
| **네트워크 정책** | 허용 목록만 통과. 엔드포인트(host·port·protocol)+L7 규칙(method·path)+허용 실행파일(binaries) | `access: full`·와일드카드 호스트·추론 제공자 호스트 금지(우리 검증기가 거부) |
| **Preset** | 재사용 가능한 정책 조각(YAML). 공식 스키마 존재 | 손으로 쓰지 않고 `ToolSpec.egress` 에서 **생성** → 도구와 정책이 어긋나지 않음 |
| **`policy add` vs `policy set`** | add = 기존 정책에 병합 후 전체 제출·검증. set = **교체**(일부만 담으면 나머지 preset 이 사라짐) | 운영 변경은 add 만. 러너에 set 템플릿 없음(테스트로 고정) |
| **운영자 승인 흐름** | 막힌 요청을 운영자가 보고 허용 여부 결정 | 에이전트가 스스로 정책을 열 수 없게. 우리의 `security_lead` 도 **제안만**, 적용은 COMMIT 승인 |
| **추론 경로(`inference.local`)** | 샌드박스는 모델 키를 갖지 않고 호스트 쪽이 키를 붙여 전달 | 탈취돼도 키가 안 샌다. 그래서 egress 에 제공자 호스트를 직접 여는 것은 금지 |
| **binaries 범위** | 어떤 실행파일이 허용 호스트에 접근 가능한가 | 글롭(`python3*`)이면 **샌드박스 단위 격리**에 의존 → 결제 샌드박스에 워커 외 코드 금지 |
| **TCB(신뢰 컴퓨팅 베이스)** | 보안이 의존하는 최소 코드/구성요소 | TCB 를 작게. 호스트 필터·승인 장부·러너가 우리의 TCB(문서 `tcb-boundary.mdx` 참고) |

**설계 귀결**: 필터(호스트)와 샌드박스 정책(네트워크)은 **같은 규칙을 일부러 중복**한다. 한쪽 버그가 다른 쪽을 무력화하지 못하도록 서로 독립으로 둔다.

## 4. 안전한 개발 수명주기 (이 저장소 방식)

```
설계 ─▶ 구현 ─▶ 테스트 ─▶ CI 게이트 ─▶ 운영
 │        │        │          │           │
 │        │        │          │           └ 드리프트 점검(기동 실패), 감사 로그, 승인 흐름
 │        │        │          └ pytest + preset 드리프트 + (garak/스캐너 게이트)
 │        │        └ 결정적 테스트(ScriptedChatModel) · 레드팀 회귀 · 불변식 · 스키마 검증
 │        └ 선언형 스펙(OrgSpec/ToolSpec) → 코드가 정책을 생성, 하드코딩 금지
 └ 위협 모델 → 부작용 등급 → 필요한 샌드박스/승인 결정
```

1. **설계**: 새 도구를 만들면 먼저 *부작용 등급*, *민감도*, *필요한 egress* 를 `ToolSpec` 에 선언한다. 선언이 곧 정책의 원천이다.
2. **구현**: 금액·ID·권한 같은 값은 LLM 인자를 믿지 않고 서버에서 계산/조회한다. 외부 입력은 스키마로 검증한다(알 수 없는 인자는 버려진다 — 테스트로 확인됨).
3. **테스트**: "공격에 속은 모델"을 스크립트로 재현해 **코드 방어선만** 결정적으로 검증한다. 실제 모델의 취약성은 garak/LangSmith 평가가 맡는다(둘은 보완 관계).
4. **CI**: 불변식 위반·정책 드리프트·스키마 위반은 머지 차단. 결과가 비었거나 형식이 틀리면 **통과가 아니라 오류**(종료 코드 2).
5. **운영**: 기동 시 적용 정책 ≠ 선언 → 기동 실패. 정책 변경은 제안 → 승인 → `policy add`, 감사 로그에 해시로 남긴다.

## 5. 개념 → 코드 → 테스트 맵

| 지키려는 속성 | 코드 | 테스트 |
|---|---|---|
| 승인 없는 COMMIT 불가 | `filters/tool_wrapper.py`, `hitl.py` | `test_simulation_golden`, `test_redteam` |
| 금액 위조 불가 | `domains/payment` (intent=hold 합계) | `test_redteam::amount_*`, `invariants.amount_matches_hold` |
| 정책 밖 egress 불가 | `sandbox/nemoclaw/policy.py`, `FakeSandboxRunner` | `test_policy`, `invariants.no_egress_outside_policy` |
| 생성된 preset 이 NemoClaw 스키마를 지킴 | `policy.build_presets` | `test_nemoclaw_schema` (공식 스키마 사용) |
| 정책 드리프트 시 기동 실패 | `sandbox/nemoclaw/drift.py` | `test_nemoclaw_runner` |
| 러너가 임의 명령을 실행하지 못함 | `sandbox/nemoclaw/runner.py` | `test_nemoclaw_runner` (이름/경로 검증, set 금지) |
| 위험 코드 실행 차단 | `filters/builtin/code_guard.py` | `test_code_guard_audit` |
| 보안 에이전트는 제안만 | `domains/security` | `test_security_domain`, `scenarios/security_*.yaml` |
| 감사 로그 변조 탐지 | `security/audit.py` | `test_code_guard_audit` (변조·삭제·재정렬·HMAC) |
| 스캐너 결과를 보수적으로 해석 | `security/scanner.py` | `test_security_scanner` |
| 공격 프롬프트 방어(garak) | `security/garak_bridge.py` | `test_garak_bridge`, `test_security_gate` |

## 6. PR 보안 체크리스트 (리뷰어용)
- [ ] 새 도구에 `side_effect`/`sensitivity`/`egress` 선언이 있고, COMMIT 이면 승인 경로가 있는가
- [ ] 금액·ID·권한을 LLM 인자에서 직접 받아 쓰지 않는가(서버에서 재계산/조회)
- [ ] 필터/러너가 예외·부재 시 **닫히는가**(fail-closed) — 그 테스트가 있는가
- [ ] 새 egress 가 필요하면 `ToolSpec` 만 바꿨고, preset 생성물(`--check`)·스키마 테스트가 통과하는가
- [ ] 로그·트레이스·감사 기록에 원문 PII/비밀이 남지 않는가
- [ ] 외부 코드/스키마를 가져왔다면 `THIRD_PARTY.md` 에 출처·라이선스를 적었는가
- [ ] 보안 에이전트/자동화가 정책을 **직접 적용**하지 않고 승인을 거치는가
- [ ] 기존 테스트를 수정하지 않았는가(수정이 필요하면 설계 이탈로 문서화)

## 7. 안티패턴 (하지 말 것)
| 안티패턴 | 이유 | 대신 |
|---|---|---|
| "시스템 프롬프트에 '결제하지 마'라고 쓰자" | 모델은 속는다 | 코드 방어선(승인·상태기계) |
| 승인을 모델이 대신 판단 | 승인의 의미 상실 | 사용자/운영자만, 1회용 |
| `policy set` 으로 일괄 교체 | 다른 preset 이 조용히 삭제 | `policy add`(병합·검증) |
| 샌드박스에 API 키 주입 | 탈취 시 키 유출 | `inference.local` 경유 |
| 테스트에서 실패 시 "스킵/통과" | 보안 구멍을 가림 | 결과 없음·파싱 실패 = 오류 |
| 외부 스캐너 결과를 그대로 신뢰 | 오탐/미검증 | needs_validation 보존, 위치 없는 confirmed 격하 |
| 하위 에이전트 보고 값 그대로 사용 | handoff 오염 | 원천에서 재계산 |

## 8. 용어집
- **egress**: 샌드박스에서 밖으로 나가는 네트워크 연결. **L7 규칙**: HTTP method/path 수준 허용.
- **drift**: 선언(소스)과 실제 적용 상태의 불일치. **fail-closed**: 불확실하면 차단.
- **taint**: 데이터 출처 라벨(신뢰 못 하는 입력에서 온 값). **HITL**: human-in-the-loop.
- **불변식**: 모든 실행에서 참이어야 하는 속성. **회귀**: 이전에 막던 것이 다시 뚫리는 것.
- **TCB**: 보안 보장이 의존하는 최소 구성요소.

## 9. 더 읽을 것
[`STUDY_NEMOCLAW_SECURITY.md`](STUDY_NEMOCLAW_SECURITY.md) · [`STUDY_LANGSMITH_EVAL.md`](STUDY_LANGSMITH_EVAL.md) · [`SECURITY_ROADMAP.md`](SECURITY_ROADMAP.md) · [`NEMOCLAW_REALITY.md`](NEMOCLAW_REALITY.md) · 포크의 `docs/security/*`(best-practices, filesystem/process/gateway controls, tcb-boundary)
