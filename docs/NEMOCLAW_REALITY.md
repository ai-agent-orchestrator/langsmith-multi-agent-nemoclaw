# NemoClaw 실측 — 문서·스키마·소스 + 실설치(2026-11-27)

> **방법**: §1–2 는 포크 `ai-agent-orchestrator/NemoClaw`(commit `ccf8e4e`, alpha)의 `docs/`, `schemas/`, `src/` 기준.
> §3 은 2026-11-27 WSL2 실설치(nemoclaw v0.0.145)에서 실측하고, 같은 버전 소스(`NVIDIA/NemoClaw` 태그 `v0.0.145`)와 대조했다.
> ✅ = 문서/스키마/소스로 확인, ⚠️ = 문서에 근거하나 **설치본 실출력 대조 필요**, ❌ = 이전 가정이 틀렸음(수정 완료).

## 1. 이전 가정 vs 실제

| 항목 | 이전 가정 | 실제 | 조치 |
|---|---|---|---|
| ❌ preset 적용 명령 | `nemoclaw <sb> policy-add <preset>` (하이픈) | `nemoclaw <sb> policy add <preset> [--yes]` (공백). 사용자 정의는 `--from-file <path>` / `--from-dir <dir>`, 미리보기 `--dry-run`. 비대화(스크립트)에서는 **프리셋 이름/파일 + `--yes`** 필요(또는 `NEMOCLAW_NON_INTERACTIVE=1`) | `runner.CommandTemplates.policy_add_file` 수정 |
| ❌ 정책 조회 | `openshell policy get --full` | `nemoclaw <sb> policy get` (내부적으로 `openshell policy get --base` 실행, 헤더 제거·자격증명 `[STRIPPED_BY_MIGRATION]`로 치환). 적용 목록은 `policy list` | `policy_get`/`policy_list` 템플릿 |
| ❌ 적용된 preset 식별 | YAML 의 `preset:` 줄 | 사용자 정의 preset 은 live 정책의 **네임스페이스 키** `nemoclaw_custom__<preset>__<원래키>` 로 들어가며 `policy list` 가 이 키에서 이름을 복원 (NemoClaw 는 별도 레지스트리를 두지 않음) | `parse_presets_from_policy` 재작성 |
| ❌ 전체 교체 | `openshell policy set` 도 후보 | `policy set` 은 **병합이 아니라 교체** — 파일에 일부만 있으면 나머지 preset 이 조용히 사라짐(OpenShell 0.0.72+). 반면 `policy add` 는 파싱된 base 문서에 병합해 **전체 문서를 제출하고 결과를 검증** | `policy set` 템플릿 금지 유지(테스트로 고정) |
| ❌ 샌드박스 이름 | 임의 | **1~19자, 소문자 시작, 영숫자·단일 하이픈, 영숫자로 끝남**, CLI 예약어(`status`,`list`,`debug`…) 금지 | `SANDBOX_NAME_RE`, ToolSpec 검증 |
| ✅ preset 파일 형식 | `preset:{name,description}` + `network_policies` | 맞음. 스키마 `policy-preset.schema.json`: 최상위 `preset`·`network_policies` 필수, 추가 속성 금지 | 공식 스키마를 `vendor/` 에 복사, 생성물 4개 보안모드 검증 테스트 |
| ✅ 엔드포인트 형식 | host/port/protocol/enforcement/rules | `protocol: rest` 는 `rules`(`allow:{method,path}`) 또는 `access` 중 하나 필수; `binaries` 필수·최소 1개(`path` 는 `^/` 로 시작); `port` 와 `ports` 는 동시 불가 | 생성기 출력이 스키마 통과 |
| ✅ 기본 정책 | deny-by-default | 보안 계층 5개(network / filesystem / process / gateway auth / inference) 모두 deny-by-default. 일부는 생성 시 고정(재생성 필요), 일부는 hot-reload | 학습 문서와 일치 |
| ✅ 추론 경로 | `inference.local` | 문서에서 `https://inference.local/v1` 확인 | 금지 호스트 목록 유지 |
| ✅ 상태/진단 | — | `nemoclaw <sb> status --json`, `nemoclaw <sb> doctor --json`(실패한 점검은 **비0 종료** → 준비 게이트로 사용 가능), `nemoclaw --version` | `doctor()` 가 종료 코드를 게이트로 사용 |

## 2. 러너가 지키는 규칙 (코드와 일치)
- 명령은 템플릿 allowlist 로만 실행, `shell=False`, 인자는 형식 검증(샌드박스 이름·preset 이름·절대경로 `.yaml`).
- 정책 변경은 `policy add` 만. 전체 교체 명령 템플릿 없음.
- `policy get` 출력이 파싱 불가 → 적용 preset 목록이 비어 → 드리프트 점검이 `missing presets` 로 **기동 실패**(fail-closed).
- 워커 전송(N2)이 없으면 `exec` 는 `sandbox_unavailable` (실행하지 않음).

## 3. 실물 확인 결과 (2026-11-27, nemoclaw v0.0.145 / openshell 0.0.116, WSL2)

> 실측: `scripts/nemoclaw_probe.sh`, 샌드박스 `my-assistant`(OpenClaw, ollama `qwen3:1.7b`). 실제 출력은 `tests/fixtures/nemoclaw/`.
> 알려진 문제와 우회 방법은 [`NEMOCLAW_KNOWN_ISSUES.md`](NEMOCLAW_KNOWN_ISSUES.md).

| # | 항목 | 결과 |
|---|---|---|
| 1 | 정책 조회 출력 형식 | ✅ `openshell policy get --base <sb>`: `Version/Hash/Status/Source/Config rev` 헤더 + `---` + YAML. 내장 프리셋은 **이름 그대로 키**(`pypi`, `brew-balanced` …). `--base`에는 `_provider_*` 항목이 없다(`--full`/`status`에만 있음). `nemoclaw <sb> policy get`도 있지만 샌드박스 잠금을 잡아서 느리다 → 래퍼는 `openshell`을 쓴다 |
| 1b | 커스텀 프리셋 키 `nemoclaw_custom__<preset>__<key>` | ⚠️ 아직 미확인. `--dry-run`은 정책 이름만 보여 준다(예: `orchestra_langsmith`, `orchestra_payments_payments_example_invalid_443`). 실제 키는 승인된 적용 1회 후 `openshell policy get --base`로 확인해야 한다 |
| 2 | `--from-file` 프리셋 이름 결정 | ✅ dry-run 이 `[orchestra-langsmith]`, `[orchestra-payments-egress]`로 표시(파일의 `preset.name`과 파일명이 같아 구분은 안 됨). 두 프리셋 모두 형식 검증 통과. 생성기는 파일명과 `preset.name`을 같게 만들므로 어느 쪽이든 현재는 무해. 공식 문서상 `preset.name`은 소문자 RFC 1123, 내장 프리셋 이름과 겹치면 거부 |
| 3 | `binaries` 글롭 | ✅ 허용. 실제 정책에 `/usr/bin/python3*`, `/sandbox/.uv/python/**/python*` → 격리 단위는 샌드박스 |
| 4 | LangSmith 경로 규칙 | ⚠️ 미확인(로컬 수신 서버 캡처 필요) |
| 5 | 샌드박스 내부 실행 | ✅ 문법 확인: `nemoclaw <sb> exec [--workdir] [--timeout <s>] [--stdin] [--tty] -- <cmd>`. 샌드박스 사용자(`HOME=/sandbox`)로 실행, 원격 종료 코드를 그대로 반환, stdout/stderr 분리, 파이프 입력은 `--stdin` 명시 필요. ✅ 실행 확인(약 1초): `exec -- id` → `uid=998(sandbox)`, `exec -- python3 --version` → 3.13.5, 종료 코드 0 |
| 6 | 버전 | ✅ nemoclaw v0.0.145, openshell 0.0.116(≥0.0.72), OpenClaw v2026.10.5 |
| 7 | 정책 변경 문법 | ✅ `nemoclaw <sb> policy add [preset] [--yes] [--dry-run] [--from-file] [--from-dir] [--trusted-private-host]`(공백 문법). `policy-add`(하이픈)는 구버전 문서 표기. 비대화 실행은 `--yes` 필요 |
| 8 | `openshell policy update` | ✅ 존재(부분 변경). `policy set`은 전체 교체 → 계속 금지 |
| 9 | 상태 | ✅ `nemoclaw <sb> status --json`(약 2초, 스키마 `schemaVersion: 1`, `policies`, `gatewayState`, `failureLayer`) vs 텍스트 `status`(실제 추론 요청 포함, 느림) |
| 10 | 잠금 | ✅ 소스 확인: 호스트 잠금 약 12초 재시도 후 실패, 샌드박스별 잠금 기본 30분 무출력 대기. 다른 PID 네임스페이스의 주인은 회수하지 않음 |
| 10b | 추론 상태 | ❌(환경) `status`/`doctor` rc=1: 샌드박스 추론 요청이 HTTP 502. Ollama(11434)는 정상, 인증 프록시(127.0.0.1:11435) 미기동. 조치: `nemoclaw my-assistant connect`로 재기동. 개발·테스트는 ScriptedChatModel 이라 영향 없음 |
| 11 | 텔레메트리 | ✅ 문서: v0.0.141+ 기본 켜짐, `NEMOCLAW_DISABLE_TELEMETRY=1`로 끔. 래퍼는 기본으로 끈다 |

### 점검 절차
```bash
bash -l scripts/nemoclaw_lock_doctor.sh       # 잠금 상태 먼저
bash -l scripts/nemoclaw_probe.sh <sandbox>   # 읽기 전용 실측 (중복 실행 방지, 잠금 감지 시 중단)
bash -l scripts/nemoclaw_hang_diag.sh <sandbox> policy-list   # 멈춤 지점 진단
```

## 4. 소스 위치(재확인용)
`docs/reference/commands.mdx`(policy get/add/list/remove, status, doctor) · `docs/network-policy/*` ·
`docs/security/best-practices.mdx` · `schemas/policy-preset.schema.json`, `network-policy.schema.json` ·
`src/lib/policy/index.ts`(`customPolicyKey`, `namespaceCustomPresetContent`).

## 5. LangSmith SDK 실측 메모 (langsmith 0.16.0)
- `evaluate(data=...)` 는 dict 가 아니라 `langsmith.schemas.Example` 객체를 요구한다 → `evals.to_sdk_examples`.
- `upload_runs=False`(= `upload_results` 별칭), `tracing_context(enabled=False)`, 루프백 클라이언트를 모두 써도 내부 run 전송/재시도가 멈추지 않아 **5분 타임아웃**이 났다.
  → 무네트워크 평가는 SDK 를 쓰지 않는 `run_local`, SDK 경로(`run_experiment`)는 **업로드 전용**(키 없으면 거부).
- 의미: 보안 모드 `closed` 에서 `evaluate` 를 호출하면 트레이스가 밖으로 나가려 한다 → 호출 금지(문서·코드 주석에 명시).
