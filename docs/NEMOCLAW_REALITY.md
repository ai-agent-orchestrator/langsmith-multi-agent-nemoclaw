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

### 3.1 지속형 워커 경로 (N2) — 소스 확인 + SSH 전제 실측 (워커 왕복 실측은 §3.2)

> 대조한 소스: `NVIDIA/NemoClaw` 태그 `v0.0.145`(`6f36fef`), `NVIDIA/OpenShell` 0.0.116(`d1155aa`).

| # | 항목 | 확인 내용 | 근거 파일 |
|---|---|---|---|
| 12 | ❌ `exec` 한 번 띄우고 줄 단위 주고받기 | TTY 가 아닌 `openshell sandbox exec`는 입력을 **EOF 까지 모두 읽은 뒤**(상한 4MiB) 명령을 시작한다. `nemoclaw exec`는 이 명령을 호출하므로 같다 | `crates/openshell-cli/src/run.rs` `sandbox_exec_grpc` |
| 13 | ❌ `nemoclaw exec` 상시 실행 | 원격 명령이 끝날 때까지 호스트 펜스와 샌드박스별 잠금 안에서 실행한다 → 상시 워커를 띄우면 다른 nemoclaw 명령이 막힌다 | `src/commands/sandbox/exec.ts`, `src/lib/actions/sandbox/lifecycle/lock.ts` |
| 14 | ✅ OpenShell SSH | **[실측]** `openshell sandbox ssh-config <sb>` + 시스템 `ssh -T` 는 입력을 도착하는 대로 전달한다. **[소스]** 샌드박스 쪽은 `/bin/bash -lc <명령>`을 정책·네트워크 네임스페이스·Landlock 적용 후 실행한다 | `crates/openshell-cli/src/ssh.rs`, `crates/openshell-supervisor-process/src/ssh.rs` |
| 15 | ⚠️ 세션 환경변수 | 자식 환경을 비우고(`env_clear`) 기본값·sandbox spec 환경·프록시·CA·provider 값만 넣는다 → 이미지 `ENV`는 전달되지 않는다고 본다. `Dockerfile.worker`는 `/etc/profile.d/`에도 같은 값을 쓴다 | 같은 파일 `apply_child_env` |
| 16 | ⚠️ SSH 인증 | 샌드박스 SSH 서버는 `none`·공개키 인증을 모두 수락하고 호스트 키를 고정하지 않는다. 접근 통제는 게이트웨이를 거치는 `ssh-proxy`가 맡는다 | 같은 파일 `auth_none`, `auth_publickey` |

실설치 확인(읽기 전용, nemoclaw 잠금 미사용):
```bash
bash -l scripts/nemoclaw_ssh_stream_check.sh <sandbox>   # STREAMING=PASS 이고 추가 stdout 0 바이트여야 한다
```

실측 결과 (2026-11-27, 샌드박스 `my-assistant`, 워커 미설치):

| 항목 | 결과 |
|---|---|
| `ssh-config` | Host `openshell-my-assistant.default`, ProxyCommand `openshell ssh-proxy --gateway-name nemoclaw --name my-assistant --workspace default` |
| 세션 | 사용자 `sandbox`(uid 998), 작업 디렉터리 `/sandbox` |
| 로그인 셸 | `/etc/profile`이 `profile.d`를 읽도록 되어 있음, 명령 외 stdout 0 바이트 |
| 줄 단위 왕복 | 입력을 닫기 전에 3회 모두 응답 (`STREAMING=PASS`) |
| 워커 | `ORCHESTRA_WORKER_*` 0개, `/opt/orchestra/venv` 없음(미설치라 정상) |

점검 스크립트는 `ssh-config` 그대로 접속한다. 런처의 더 좁은 옵션(`none` 인증만 허용)과 설정 파서는 실설치에서 아직 실행하지 않았다.

### 3.2 워커 샌드박스 설치 (N2) — 소스 확인 + 컨테이너 대역 검증 + **실설치 생성·워커 왕복 확인**

| # | 항목 | 확인 내용 | 근거 |
|---|---|---|---|
| 17 | ❌ `nemoclaw onboard --from nemoclaw/Dockerfile.worker` | `--from` 이미지는 NemoClaw 관리 런타임까지 포함한 완전한 이미지여야 한다. 워커 이미지로는 기동 실패(exit 127). → `openshell sandbox create --from`으로 만든다 | NemoClaw `docs/reference/troubleshooting.mdx` |
| 18 | 빌드 방식 | 컨텍스트는 Dockerfile 이 있는 디렉터리, build arg 전달 없음 → 스크립트가 git 추적 파일만 임시 디렉터리에 모으고 베이스 digest 를 채운다 | OpenShell `run.rs` `resolve_from` |
| 19 | 이미지 요건 | root 가 아닌 `USER`, 쓰기 가능한 `/sandbox`, `iproute2`. `CMD`는 쓰이지 않고, 주 프로세스가 끝나면 샌드박스가 오류 상태가 된다(`sleep infinity`) | `examples/bring-your-own-container`, `docs/sandboxes/manage-sandboxes.mdx` |
| 20 | 파일 접근 | 정책에 없는 경로는 접근 불가 → `nemoclaw/policy-worker.yaml`: 네트워크 없음, `/opt/orchestra` 읽기 전용, `/sandbox` 쓰기, Landlock `hard_requirement` | `docs/reference/policy-schema.mdx` |
| 21 | ❌ 네트워크 규칙이 없는 정책 | `policy get` 출력에서 빈 `network_policies`는 **생략**된다. 이전 파서는 이를 거부해 모든 실행이 `policy_unrecognized`가 됐을 것 → OpenShell 헤더가 있는 정책 문서일 때만 "규칙 없음"으로 읽는다 | `crates/openshell-policy/src/lib.rs` `PolicyFile` |
| 22 | 인증 키 | 호스트의 마스터 키는 밖으로 나가지 않는다. 샌드박스에는 `HMAC(마스터, 샌드박스 이름)` 파생 키만 SSH 표준입력으로 넣는다(`/sandbox/state/capability.key`, 0400) | `orchestra/security/capability.py` |

검증한 범위: 실제 Docker(classic builder)로 이미지 빌드 → 환경을 비운 SSH 세션을 흉내 낸 컨테이너에서 설치 스크립트의 생성·키 설치·워커 점검, `-m nemoclaw` 테스트 통과. 베이스는 레지스트리 접근이 막혀 **Ubuntu 24.04 + Python 3.12 로컬 이미지**를 썼다(`python:3.11-slim` 아님).
**실설치 실측 (2026-11-27, WSL2 Ubuntu 26.04, 호스트 Python 3.14, 사용자 실행):** `scripts/nemoclaw_worker_sandbox.py --yes` 가 `WORKER_PROBE=PASS`.
- 베이스 `python@sha256:61e01f72680f86a5279ad510818ab7b2ee9f049822589d4e34e33e044360356c`(`python:3.11-slim`)로 247개 파일 컨텍스트에서 이미지 빌드, 게이트웨이 `nemoclaw`의 로컬 Docker 에서 샌드박스 `orchestra-code` 생성.
- SSH 표준입력으로 파생 키 설치, 지속형 transport 한 프로세스로 점검 요청 3회 모두 `worker_tool_forbidden`(기대값). 왕복 0.601초(첫 회, 워커 기동 포함) → 0.003초 → 0.002초.
- 호스트에 `python3.14-venv` 패키지가 없으면 가상환경 생성이 실패한다(`sudo apt install -y python3.14-venv`).

- `-m nemoclaw` 실연동 pytest `1 passed`(Python 3.14).
- `openshell policy get --base orchestra-code`: `Status: Effective`, `Source: sandbox`. 내용은 `nemoclaw/policy-worker.yaml`과 같고, 빈 `network_policies`는 **출력에서 생략**됨(#21 실측 확인). 저장소 파서에 이 출력을 넣어 `network_policies` 없음, drift 0건 확인.
- 지속형 transport 의 왕복 시간에는 러너가 실행 전마다 하는 `openshell policy get`(1초 미만, §3)이 포함되지 않는다.

**확인하지 못한 것:** 네트워크 차단·Landlock 이 실제로 막는지, 워커 재시작·타임아웃 동작, NemoClaw 가 관리하지 않는 샌드박스가 기존 샌드박스에 주는 영향.

설치 절차 (**WSL2 셸 안에서** — PowerShell 이면 먼저 `bash -l`. 샌드박스 생성은 사용자 승인 후):
```bash
git clone -b 21-nemoclaw-cli https://github.com/ai-agent-orchestrator/langsmith-multi-agent-nemoclaw ~/orchestra && cd ~/orchestra
python3 -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'
docker pull python:3.11-slim
BASE="$(docker image inspect python:3.11-slim --format '{{index .RepoDigests 0}}')"; echo "$BASE"   # 검토 후 사용
python scripts/nemoclaw_worker_sandbox.py --python-base "$BASE"          # 미리보기, 생성 안 함
python scripts/nemoclaw_worker_sandbox.py --python-base "$BASE" --yes    # 생성 + 키 설치 + 점검
ORCHESTRA_NEMOCLAW_LIVE_SANDBOX=orchestra-code pytest -m nemoclaw tests/test_nemoclaw_persistent.py
openshell sandbox delete orchestra-code                                  # 되돌리기
```

**남은 제약**
- 2026-11-27 에 만든 `orchestra-code` 이미지에는 툴이 없다. 읽기 툴은 §3.3, 이미지를 다시 만들어야 들어간다.
- 샌드박스 사용자로 실행되는 코드는 그 샌드박스의 파생 키를 읽을 수 있다. 다른 샌드박스의 요청은 서명할 수 없다.
- 워커 이미지의 샌드박스 이름·조직 파일은 `orchestra-code`/`claude_dev` 고정이다.
- 호스트에서 지속형 transport 를 쓰려면 `NEMOCLAW_TRANSPORT=ssh_stream`, `NEMOCLAW_EXEC_VERIFIED=1`, `ORCHESTRA_CAPABILITY_KEY_FILE=<마스터 키 파일>`.

### 3.3 워크스페이스 읽기 툴 — 오프라인 확인, **실설치 미확인**
| 항목 | 내용 |
|---|---|
| 툴 | `fs_read`·`fs_list`·`fs_glob`·`fs_grep`. READ 등급, `sandbox: required`, 샌드박스 `orchestra-code`, 네트워크 선언 없음 |
| 권한 | `claude_dev`의 `feature-dev/code-explorer`(`code_explorer`)만 |
| 경로 제한 | `ORCHESTRA_WORKSPACE` 아래 상대경로만. 심볼릭 링크는 어느 단계에서도 따라가지 않음(디렉터리 fd 기준 `O_NOFOLLOW`). 하드 링크가 있는 파일·특수 파일·비 UTF-8 거부. 설정이 없거나 `/`면 실패 |
| 숨김 | `.git`·`.ssh`·`.env*`(`.env.example` 제외)·`*.pem`·`*.key` 등 이름 기반. 다른 이름의 비밀 파일은 가리지 못함 |
| 상한 | 파일 2MB, 반환 10만 자, 항목 500, 일치 100, 검색 5,000 항목·20MB·10초 |
| 확인한 범위 | 러너(정책 확인·서명) → 실제 워커 프로세스 → pre/post 필터. 이미지의 `requirements-worker.lock` 의존성으로 워커 실행(Python 3.11). 이미지 빌드는 이번 변경 뒤 다시 하지 않음 |

실설치 확인 (WSL2 셸, 저장소 최신 상태에서. 샌드박스 삭제·재생성은 사용자 승인 후):
```bash
cd ~/orchestra && git pull && . .venv/bin/activate
openshell sandbox delete orchestra-code
BASE="$(docker image inspect python:3.11-slim --format '{{index .RepoDigests 0}}')"; echo "$BASE"
python scripts/nemoclaw_worker_sandbox.py --python-base "$BASE" --yes
ORCHESTRA_NEMOCLAW_LIVE_SANDBOX=orchestra-code ORCHESTRA_CAPABILITY_KEY_FILE="$HOME/.config/orchestra/capability.key" \
  pytest -m nemoclaw tests/test_workspace_sandbox.py
```
합격: `WORKER_PROBE=PASS`, pytest `1 passed`. 이 시험은 빈 워크스페이스 목록과 워크스페이스 밖 경로 거부를 본다. 네트워크 차단·Landlock 은 확인하지 않는다.

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
