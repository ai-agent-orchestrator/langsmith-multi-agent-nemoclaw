# NemoClaw 보완 예정 (로드맵)

> 현재 상태: **정책 생성·검증 계층까지 구현·테스트 완료, 실제 NemoClaw/OpenShell 연동은 미검증.**
> 이 환경에는 `nemoclaw`/`openshell` 바이너리와 샌드박스가 없어서, 아래 "미검증" 항목은 NVIDIA 공식 문서(docs.nvidia.com/nemoclaw)를 근거로 설계했을 뿐 실제로 실행해 보지 못했다. 구현 전 반드시 설치된 버전의 `--help` 와 `openshell policy get --full` 출력으로 확인한다 (`[VERIFY]`).

## 1. 왜 NemoClaw 를 붙이나 (역할 분담)

| 계층 | 담당 | 막는 것 |
|---|---|---|
| L0 요청 게이트 / L1 FilterChain | 앱(`orchestra/filters`) | 대량 PII·인젝션·금액 한도·상태 위반 |
| L2 HITL 승인 | 앱(`hitl.py`, 툴 래퍼) | 승인 없는 결제(COMMIT) |
| **L3 샌드박스 + egress 정책** | **NemoClaw / OpenShell** | 앱이 뚫려도 **네트워크·파일·프로세스 밖으로 못 나감** (기본 전부 거부) |
| L4 트레이스 마스킹/차단 | LangSmith 연동 | 민감정보의 외부 전송 |

핵심 설계: **egress 정책을 손으로 쓰지 않고 `ToolSpec.egress` 에서 생성**한다 → 툴 선언·정책·LangSmith 메타데이터가 어긋나지 않는다.

## 2. 현재 구현 상태

| 항목 | 상태 | 위치 / 비고 |
|---|---|---|
| ToolSpec 의 sandbox/egress 선언 | ✅ | `orchestra/specs.py`, `domains/payment/pack.py` |
| 신뢰 경계별 샌드박스 분리(`orchestra-payments`, 이후 `orchestra-code`) | ✅ 설계·검증 규칙 | 같은 host:port 가 두 샌드박스에 열리면 검증 실패 |
| preset YAML 생성 | ✅ | `policy.build_presets` → `nemoclaw/presets/*.yaml` (커밋됨, CI drift 검사) |
| 금지 규칙 검증 | ✅ | 추론 호스트 금지, `binaries` 필수, `access: full` 금지, `allowed_ips` 금지, 와일드카드 호스트 금지, L7 규칙 필수, placeholder 호스트(`*.invalid`) 금지(`--strict`) |
| 보안모드 ↔ preset 대응 | ✅ | `closed` 는 LangSmith preset 을 **아예 생성하지 않음**(앱 버그로 트레이싱이 켜져도 egress 에서 차단) |
| 기동 시 fail-closed 점검 함수 | ✅ 함수만 | `policy.check_applied` — **서비스 기동에 아직 연결 안 됨**(N3) |
| 샌드박스 계약 + Fake 러너 | ✅ | `sandbox/base.py`, `fake.py` (egress 거부·hot-reload revision 흉내) |
| 툴 래퍼의 샌드박스 라우팅 + fail-closed | ✅ | 러너 없는데 `nemoclaw` 모드면 `sandbox_unavailable` 로 중단 |
| egress 거부 시나리오 | ✅ | `scenarios/sandbox_egress_denied.yaml` |
| **실제 NemoClaw 러너 / CLI 래퍼** | ⬜ | N1 |
| **샌드박스 내부 워커 + 전송 방식** | ⬜ | N2 |
| **기동 시 preset 일치 점검, `doctor`** | ⬜ | N3 |
| **추론 라우팅(`inference.local`) / 프라이버시 라우터** | ⬜ | N4 |
| **감사 이벤트 → LangSmith** | ⬜ | N5 |
| **코드 실행 샌드박스(Claude dev 에이전트) / Deep Agents Code** | ⬜ | N6 |
| 운영 절차·이미지(`Dockerfile.worker`, `nemoclaw/README.md`) | ⬜ | N7 |

## 3. 단계별 계획 (Codex, 순서대로)

### N1. 실제 러너 — `orchestra/sandbox/nemoclaw/cli.py`, `runner.py`
- 명령 템플릿을 **상수 테이블**로 두고 `shell=False`, 인자 검증, timeout, `dry_run` 지원. 허용 명령: `onboard`, `<name> connect`, `<name> policy-add <preset>`, `<name> policy-list`, `<name> status`, `<name> logs`, `openshell policy get --full`, `openshell policy set --policy <file> --wait`, `openshell term`(읽기 전용 안내만).
- `doctor()`: 바이너리 존재, `--help` 로 서브커맨드 존재 확인, 샌드박스 존재, 적용 preset, 추론 경로를 표로 출력.
- 테스트는 `dry_run` 명령 조립 + 가짜 바이너리(스크립트)로 stdout 파서 검증. 실제 바이너리 테스트는 `-m nemoclaw`.
- **완료 조건**: 바이너리 없이 `pytest` 통과, 있으면 `-m nemoclaw` 로 `doctor` 성공.

### N2. 워커와 전송 — `worker.py`, `transport/`
- 샌드박스 안에서 `ExecRequest` JSON 을 받아 **허용 툴 테이블**만 실행(임의 코드 실행 금지), 실행 전 **같은 FilterChain(pre_tool) 재실행(이중 검사)**.
- `NEMOCLAW_TRANSPORT=stdio_via_cli | http_forward` 를 플러그형으로. 어느 쪽이 실제로 가능한지는 `[VERIFY]`(OpenShell 이 제공하는 포워딩/`connect` 셸 사용 방식).
- `ExecRequest.trace_headers` 로 호스트 run 트리의 자식으로 이어붙이기(LangSmith 분산 트레이싱).
- **완료 조건**: Fake 전송으로 왕복 테스트, 멱등 키 재전송 시 중복 실행 없음.

### N3. 기동 시 정책 일치 검사 + 운영 점검
- `OrchestraService` 시작 시 `nemoclaw`/`nemoclaw-full` 모드면 `policy-list` 결과를 `policy.check_applied` 에 넣어 **불일치면 기동 실패**.
- `closed` 모드에서 `orchestra-langsmith` 가 적용돼 있으면 실패(이미 함수에 구현됨, 연결만).
- CI: `gen_nemoclaw_presets.py --check`(이미 있음) + `--strict`(운영 배포 파이프라인, `PAYMENT_PROVIDER_HOST` 필수).
- LangSmith preset 의 경로 규칙(`/runs/**` 등)은 **추정값**이다 → 로컬 수신 서버로 SDK 요청 경로를 캡처해 rules 를 자동 생성하도록 수정.

### N4. 추론 라우팅과 프라이버시 라우터
- 샌드박스 안에서는 모델 호출을 `inference.local` 로 보낸다(자격증명은 호스트에 남음). `NEMOCLAW_INFERENCE_BASE_URL` 은 **환경변수 필수**(경로·스킴 `[VERIFY]`, 기본값 하드코딩 금지).
- `AliasModelFactory.builder` 구현: provider(`anthropic` / `openai_compatible` / 로컬 Nemotron·vLLM·Ollama·NIM).
- `route_for_sensitivity`(이미 구현)로 `pii/pci` 는 로컬 모델 우선, 결정 결과를 run metadata `model_route` 로 기록. NemoClaw 게이트웨이의 라우팅과 **이중 결정**(L3 실패 대비).
- **금지**: 추론 제공자 호스트를 egress preset 에 넣지 않는다(검증 규칙으로 이미 차단) — 관리형 자격증명 경계를 우회하기 때문.

### N5. 감사 → LangSmith
- `ExecResult.egress_denied`, `nemoclaw <name> logs`, `openshell term` 출력에서 deny/approve 이벤트를 추출 → LangSmith feedback `nemoclaw_egress_denied`, metadata `policy_revision`, annotation queue 투입.
- **미등록 호스트 요청은 NemoClaw 가 운영자 승인 대기로 올린다.** 앱은 이를 사용자에게 "운영자 승인이 필요" 로 안내만 하고, **앱이 스스로 정책을 열지 않는다**(시나리오 `sandbox_egress_denied` 가 이 동작).

### N6. 코드 실행 샌드박스 (`orchestra-code`) — Claude dev 에이전트용
- `Write/Edit/Bash` 계열 툴(C6)을 `orchestra-code` 샌드박스에서만 실행. egress 는 패키지 인덱스 등 최소 목록, **결제 호스트는 절대 포함 금지**(교차 검증 이미 있음).
- 파일시스템(Landlock)·프로세스 계층은 **샌드박스 생성 시 고정**(재생성 필요) → `Dockerfile.worker`/onboard 인자로 정의. 작업 폴더는 `/sandbox` 하위.
- 선택: LangChain **Deep Agents Code**(`nemo-deepagents` 변형, `dcode`)를 코드 에이전트 런타임으로 쓰고 OTLP 트레이싱을 LangSmith OTel 로 연결(`[VERIFY]`).

### N7. 운영 문서·이미지
- `nemoclaw/README.md`: preset 생성 → NemoClaw 소스 트리 `nemoclaw-blueprint/policies/presets/` 복사(파일명 = `preset.name`) → onboard → `policy-add` → `doctor` → 모니터링.
- `nemoclaw/Dockerfile.worker`, `NEMOCLAW_DISABLE_TELEMETRY=1`, `NEMOCLAW_POLICY_TIER` 비대화형 설정.

## 4. 알려진 한계·위험 (보완 시 판단 필요)

1. **`binaries` 가 `python3*` 글롭이다.** 샌드박스 안에서 "그 파이썬이면 어떤 코드든 해당 egress 를 쓸 수 있다"는 뜻이라, **실행파일 단위 격리가 아니라 샌드박스 단위 격리**에 의존한다. 그래서 결제 샌드박스에는 워커 외 코드를 두지 않는다. 가능하면 전용 워커 바이너리 경로 + 첫 사용 SHA256 검증으로 좁힌다.
2. **`openshell policy set` 은 정책 전체를 교체한다**(preset 메타 블록도 못 받음). 운영 변경은 `nemoclaw <name> policy-add <preset>` 으로만 하고, 원시 교체는 `policy get --full` 스냅샷에서 시작한다.
3. **계층별 변경 가능 여부가 다르다.** 네트워크·추론은 실행 중 반영(hot-reload), **파일시스템·프로세스·게이트웨이 인증은 샌드박스 재생성** 필요 → 이미지/onboard 단계에서 확정해야 한다.
4. **정책 리비전 추적**: `ExecResult.policy_revision` 을 트레이스에 남겨 "어느 정책 버전에서 거부됐는지" 재현 가능하게 한다.
5. **프로세스 메모리 장부**: 승인·결제 장부가 메모리라 샌드박스 재시작/다중 프로세스에서 일관성이 없다 → 영속 저장소 필요(워커 도입 시 N2 에서 함께 결정).
6. NemoClaw 는 **초기(early preview) 오픈소스**로 릴리스가 빠르다. CLI·스키마는 버전마다 바뀔 수 있어 명령 템플릿 상수화 + `doctor` 로 방어한다.

## 5. 실제 NemoClaw 가 설치된 환경에서 가장 먼저 확인할 체크리스트

- [ ] `nemoclaw --help`, `openshell --help` 의 서브커맨드가 N1 허용 명령과 일치하는가
- [ ] `openshell policy get --full <sandbox>` 의 스키마가 `policy.build_presets` 출력(`network_policies` → `endpoints`/`binaries`)과 일치하는가
- [ ] preset 파일을 `policy-add` 했을 때 검증을 통과하는가 (`protocol: rest`, `enforcement: enforce`, `rules: - allow: {method, path}`)
- [ ] `binaries` 글롭(`python3*`)이 허용되는가, 아니면 정확한 경로가 필요한가
- [ ] 샌드박스 안에서 `inference.local` 의 base URL·경로·스킴
- [ ] 호스트→샌드박스로 워커를 호출하는 실제 방법(stdio / 포워딩)
- [ ] LangSmith SDK 가 실제로 치는 경로가 `LANGSMITH_RULES` 로 충분한가
- [ ] 미등록 호스트 요청이 앱에서 어떤 오류로 보이는가(타임아웃 vs 즉시 거부) → `ExecResult.egress_denied` 채우는 방법
