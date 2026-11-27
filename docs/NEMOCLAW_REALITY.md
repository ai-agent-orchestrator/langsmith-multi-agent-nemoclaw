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

**확인하지 못한 것:** 워커 재시작 동작, NemoClaw 가 관리하지 않는 샌드박스가 기존 샌드박스에 주는 영향. 네트워크 차단·Landlock 은 이후 §3.4, 시간 초과 응답은 §3.3 에서 확인.

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
- 위 §3.2 실측 때의 이미지에는 툴이 없었다. 읽기 툴을 넣은 이미지의 실측은 §3.3.
- 샌드박스 사용자로 실행되는 코드는 그 샌드박스의 파생 키를 읽을 수 있다. 다른 샌드박스의 요청은 서명할 수 없다.
- 워커 이미지의 샌드박스 이름·조직 파일은 `orchestra-code`/`claude_dev` 고정이다.
- 호스트에서 지속형 transport 를 쓰려면 `NEMOCLAW_TRANSPORT=ssh_stream`, `NEMOCLAW_EXEC_VERIFIED=1`, `ORCHESTRA_CAPABILITY_KEY_FILE=<마스터 키 파일>`.

### 3.3 워크스페이스 읽기 툴 — 오프라인 확인 + **실설치 툴 실행 확인**
| 항목 | 내용 |
|---|---|
| 툴 | `fs_read`·`fs_list`·`fs_glob`·`fs_grep`. READ 등급, `sandbox: required`, 샌드박스 `orchestra-code`, 네트워크 선언 없음 |
| 권한 | `claude_dev`의 `feature-dev/code-explorer`(`code_explorer`)만 |
| 경로 제한 | `ORCHESTRA_WORKSPACE` 아래 상대경로만. 심볼릭 링크는 어느 단계에서도 따라가지 않음(디렉터리 fd 기준 `O_NOFOLLOW`). 하드 링크가 있는 파일·특수 파일·비 UTF-8 거부. 설정이 없거나 `/`면 실패 |
| 숨김 | `.git`·`.ssh`·`.env*`(`.env.example` 제외)·`*.pem`·`*.key` 등 이름 기반. 다른 이름의 비밀 파일은 가리지 못함 |
| 상한 | 파일 2MB, 반환 10만 자, 항목 500, 일치 100, 검색 5,000 항목·20MB·10초 |
| 확인한 범위 | 러너(정책 확인·서명) → 실제 워커 프로세스 → pre/post 필터. 이미지의 `requirements-worker.lock` 의존성으로 워커 실행(Python 3.11). 실설치 확인은 아래 |

**실설치 실측 (2026-11-27, WSL2, 호스트 Python 3.14.4, 사용자 실행):**
- 대상 코드: `fd31154` 위에 읽기 툴 커밋 2개(`2187f74`, `594810d`)의 변경만 얹은 것. **4단계(결정 계층) 병합 코드가 들어간 이미지는 실설치에서 만들지 않았다.**
- 이전 `orchestra-code` 삭제 → 248개 파일 컨텍스트에서 이미지 빌드(베이스는 §3.2 와 같은 digest) → 샌드박스 재생성 → `WORKER_PROBE=PASS`(0.653초, 0.002초, 0.002초).
- `pytest -m nemoclaw tests/test_workspace_sandbox.py` `1 passed`: 샌드박스 안에서 `fs_list` 성공, `fs_read ../state/capability.key` 는 `path_not_allowed`.

**시간 초과 응답 실측 (같은 날, 사용자 실행):**
- 대상 코드: `ec2558e`(4단계 병합 코드 포함). `orchestra-code` 를 다시 만든 뒤 `pytest -m nemoclaw tests/test_workspace_sandbox.py tests/test_nemoclaw_persistent.py` `3 passed`(점검 스크립트가 성공해야 pytest 가 실행되는 명령).
- 확인: 실행 시간이 없는 요청이 `worker_timeout` 으로 돌아오고, 같은 워커 SSH 세션이 다음 요청을 처리한다. 즉 알람 타이머와 감시 장치를 거는 코드가 샌드박스 정책 아래에서 동작한다.
- **확인하지 못한 것:** 오래 걸리는 툴을 실제로 중단하는 동작, 워커 프로세스 강제 종료와 재시작.

**확인하지 못한 것:** 파일이 있는 워크스페이스에서의 `fs_read`·`fs_glob`·`fs_grep`, 실설치에서의 링크·숨김 파일 거부. 위 거부는 툴의 경로 검사 결과이며 Landlock·네트워크 차단 확인이 아니다.

재현 명령 (WSL2 셸, 저장소 최신 상태에서. 샌드박스 삭제·재생성은 사용자 승인 후):
```bash
cd ~/orchestra && git pull && . .venv/bin/activate
openshell sandbox delete orchestra-code
BASE="$(docker image inspect python:3.11-slim --format '{{index .RepoDigests 0}}')"; echo "$BASE"
python scripts/nemoclaw_worker_sandbox.py --python-base "$BASE" --yes
ORCHESTRA_NEMOCLAW_LIVE_SANDBOX=orchestra-code ORCHESTRA_CAPABILITY_KEY_FILE="$HOME/.config/orchestra/capability.key" \
  pytest -m nemoclaw tests/test_workspace_sandbox.py
```
합격: `WORKER_PROBE=PASS`, pytest `1 passed`.

### 3.4 격리 점검 — **실설치 통과** (2026-11-27)
`python scripts/nemoclaw_isolation_check.py` (WSL2, `orchestra-code` 가 있을 때). 샌드박스를 다시 만들지 않는다. 점검 프로그램(`orchestra/sandbox/nemoclaw/isolation_probe.py`)을 워커와 같은 SSH 경로의 표준입력으로 보내 실행하고, 만들 수 있었던 파일은 지운다.

| 점검 | 통과 조건 |
|---|---|
| `control_tmp_write` | `/tmp` 쓰기 성공(대조군) |
| `direct_tcp_refused` | 프록시를 거치지 않는 TCP 연결 실패. 호스트에서도 닿지 않으면 미확인 |
| `proxy_tunnel_refused`·`proxy_http_refused` | 샌드박스 프록시가 `CONNECT`·평문 HTTP 를 403 으로 거절 |
| `write_var_tmp_refused`·`write_dev_shm_refused` | 누구나 쓸 수 있지만 정책에 없는 디렉터리에 쓰기 거부(Landlock) |
| `read_unlisted_refused` | 누구나 읽을 수 있지만 정책에 없는 파일 열기 거부(Landlock) |
| `write_code_refused` | `/opt/orchestra` 쓰기 거부 |
| `not_root`·`no_capabilities`·`no_new_privileges` | 권한 없는 프로세스 |
| `memfd_refused` | `memfd_create` 가 EPERM(seccomp) |

`ISOLATION=PASS` 는 전부 통과, `FAIL` 은 하나라도 허용됨, `INCOMPLETE` 는 확인하지 못한 항목이 있음. 판정하지 않는 정보: DNS 해석 여부, 비밀값 모양의 환경변수 **이름**.

`--inference-host inference.local` 을 주면 `inference_route_refused` 를 함께 판정한다: 모델 목록(`GET /v1/models`)을 요청해, 터널이 403 이거나 "라우트 없음" 503 일 때만 통과. 그 밖의 HTTP 응답은 라우트가 있다는 뜻이라 `FAIL`. `--send-prompt` 는 `ping` 한 단어에 1토큰 응답을 추가로 요청해 상태 코드만 보여 준다(게이트웨이가 가리키는 모델이 실행되며, 유료 서비스면 비용이 든다).
점검 범위 밖: CPU·메모리 상한, 샌드박스 간 격리, DNS 를 통한 유출.

**실설치 실측 (2026-11-27, WSL2, 사용자 실행, `--inference-host inference.local`):** `ISOLATION=PASS`. 이미지는 `ec2558e` 로 만든 `orchestra-code`, 점검 스크립트는 `48e7b6a`.

| 결과 | 관측값 |
|---|---|
| 외부 접속 거부 | 직접 TCP 는 시간 초과, 프록시 `CONNECT`·평문 HTTP 는 403 |
| 파일 정책 적용 | `/var/tmp`·`/dev/shm` 쓰기와 `/var/lib/dpkg/status` 열기가 `EACCES`. 일반 파일 권한으로는 허용되는 대상이라 Landlock 이 거부한 것 |
| 코드 쓰기 거부 | `/opt/orchestra` `EACCES`(파일 권한만으로도 거부되는 대상) |
| 권한 | uid 10001, `CapEff` 0, `NoNewPrivs` 1, `memfd_create` `EPERM` |
| 정보 | DNS 해석 실패, 비밀값 모양의 환경변수 이름 없음 |
| ⚠️ `inference.local` | 네트워크 규칙이 없어도 프록시가 터널을 연다(`status=200`) |

**모델 경로 실측 (같은 날, 사용자 실행, 점검 스크립트 `2dbfeb1`, `--send-prompt`):** `openshell inference get` 은 두 항목 모두 `Not configured`. `inference_route_refused PASS no_route`, 1토큰 요청은 `status=503`, 전체 `ISOLATION=PASS`. 지금 이 샌드박스의 요청은 어떤 모델에도 닿지 않는다. 터널 자체는 열리므로 "네트워크가 완전히 없다"고 설명하지 않는다.

**나중에 모델(Claude API 등)을 연결할 때:** 라우트를 설정하기 전에 아래 워크스페이스 분리를 검토하고, 설정한 뒤 `--inference-host inference.local` 점검을 다시 돌린다(사용자 결정: 연결 시점에 처리).

**`inference.local` — 소스 확인 (OpenShell 0.0.116. "라우트 없음" 503 만 실설치에서 확인):**
- 프록시는 네트워크 정책 평가 전에 `inference.local:443` 터널을 연다(`openshell-supervisor-network/src/proxy.rs`). 프리셋·정책으로는 막지 못한다.
- 추론 요청은 샌드박스 밖의 프록시가 게이트웨이의 라우트와 자격 증명으로 전달한다. 라우트가 있으면 샌드박스 코드는 키 없이 모델을 부를 수 있다.
- 라우트는 **OpenShell 워크스페이스 단위**다(`openshell-server/src/inference.rs` `get_inference_bundle` → 샌드박스의 워크스페이스, `docs/sandboxes/manage-workspaces.mdx`). 라우트가 없으면 503 `cluster inference is not configured`.
- 막는 방법(적용 전, 게이트웨이 설정 변경이라 사용자 승인 필요): 추론 라우트를 두지 않는 별도 워크스페이스(`openshell workspace create --name orchestra-worker`)에 워커 샌드박스를 만들고, 호스트의 OpenShell 호출에 `OPENSHELL_WORKSPACE=orchestra-worker` 를 준다. 그 워크스페이스에 나중에 라우트를 설정하면 다시 열리므로 점검으로 확인한다.

### 3.5 부하·재시작·메모리 점검과 자원 상한 — **실설치 미확인**
`python scripts/nemoclaw_load_check.py` (WSL2, `orchestra-code` 가 있고 다른 호출이 없을 때). 존재하지 않는 툴만 요청하므로 툴은 실행되지 않고 키도 필요 없다. 자기가 띄운 워커만 종료하며 정책·파일·설정은 바꾸지 않는다.

| 줄 | 보는 것 | FAIL 조건 |
|---|---|---|
| `STARTUP`·`SEQUENTIAL`·`CONCURRENT` | 첫 요청, 순차, 동시 호출자의 지연(p50·p95·최대)과 처리량 | 요청이 하나라도 실패 |
| `ONE_WORKER`·`MANY_WORKERS` | 워커 1개와 N개의 메모리(RSS), 샌드박스 안에서 보이는 합계 | 워커 수가 어긋남 |
| `AFTER_WORKER_CRASH` | 샌드박스 안 워커를 강제 종료한 뒤 두 번 안에 새 워커가 응답 | 회복 실패 |
| `AFTER_CONNECTION_LOSS` | 호스트의 SSH 만 끊었을 때 샌드박스의 워커가 15초 안에 끝남 | 워커가 남음 |
| `CLEANUP` | 점검이 끝난 뒤 남은 워커 | 워커가 남음 |

`LOAD=PASS`/`FAIL`/`INCOMPLETE`. 샌드박스 안에서 프로세스를 볼 수 없거나 신호를 보낼 수 없으면 `INCOMPLETE`다. 오프라인에서는 로컬 프로세스를 대신 세워 확인했다(`tests/test_load_check.py`). 메모리 수치에는 샌드박스 감독 프로세스처럼 보이지 않는 프로세스가 빠질 수 있으므로, 상한을 정할 때는 호스트의 `docker stats --no-stream` 값을 함께 본다.

**자원 상한 (OpenShell 0.0.116 소스·문서 확인):** `openshell sandbox create --cpu <2|0.5|500m> --memory <512Mi|2Gi>`. Docker 드라이버는 컨테이너의 `NanoCpus`·`Memory` 로 적용한다. `Memory` 만 지정하므로 Docker 기본값에 따라 호스트에 swap 이 있으면 그만큼 더 쓸 수 있다. 프로세스 수 상한은 드라이버 기본 2048 이다. 설치 스크립트는 값이 있을 때만 전달하고, 단위 없는 메모리 값은 바이트로 해석되므로 거절한다. 상한을 넘었을 때 실설치에서 어떻게 실패하는지는 확인하지 않았다.

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
