# NemoClaw 실측 (R0) — 문서·스키마·소스 기준

> **방법과 한계**: 이 환경에서는 NemoClaw 를 설치·실행하지 못했다. 대신 포크 `ai-agent-orchestrator/NemoClaw`
> (commit `ccf8e4e2522e4abc3bd7c9e371e6c78518be82ec`, alpha)의 `docs/`, `schemas/`, `src/` 를 직접 읽어 확정했다.
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

## 3. 아직 실물로 확인해야 하는 것 ⚠️
1. `nemoclaw <sb> policy get` 의 실제 출력이 `network_policies:` YAML 이고 키가 `nemoclaw_custom__…` 인지(소스 근거는 있으나 출력 샘플 미확인).
2. `--from-file` 로 적용할 때 preset 이름이 **파일 내 `preset.name`** 인지 **파일명** 인지(두 값을 같게 생성하므로 현재는 무해).
3. `binaries` 의 글롭(`python3*`)이 정책 엔진에서 실제로 허용되는지, 아니면 정확한 경로만 허용하는지.
4. LangSmith 호스트용 규칙(`/runs/**` 등)이 실제 SDK 요청 경로와 맞는지 — 로컬 수신 서버로 캡처.
5. 샌드박스 내부 워커로 요청을 보내는 방법(`nemoclaw <sb> exec …` 가 후보. 문서상 종료 코드 전달·출력 상한 64 MiB·`--workdir` 존재) — N2 설계 전에 실험.
6. 설치 버전에서 `nemoclaw --version` 과 OpenShell 버전(≥0.0.72 필요 기능 여부).

### 실물 점검 절차 (설치 후 15분)
```bash
nemoclaw --version && nemoclaw doctor                 # 호스트/게이트웨이
nemoclaw <sb> doctor --json | jq .                     # 샌드박스 준비 상태
nemoclaw <sb> policy list                              # 현재 적용 preset
nemoclaw <sb> policy add --from-file nemoclaw/presets/orchestra-payments-egress.yaml --dry-run
nemoclaw <sb> policy add --from-file nemoclaw/presets/orchestra-payments-egress.yaml --yes
nemoclaw <sb> policy get > /tmp/live.yaml              # nemoclaw_custom__ 키 확인 → ⚠️1,2 해소
```
결과(실제 출력 샘플)를 이 문서 §3 에 붙이고 ⚠️ 를 ✅ 로 바꾼다.

## 4. 소스 위치(재확인용)
`docs/reference/commands.mdx`(policy get/add/list/remove, status, doctor) · `docs/network-policy/*` ·
`docs/security/best-practices.mdx` · `schemas/policy-preset.schema.json`, `network-policy.schema.json` ·
`src/lib/policy/index.ts`(`customPolicyKey`, `namespaceCustomPresetContent`).
