#!/usr/bin/env bash
# NemoClaw 실설치 점검 (V1) — 읽기 전용.
# 샌드박스 생성(onboard), 정책 적용(policy add --yes), 정책 교체(policy set), 추론 호출은 하지 않는다.
# 정책 추가는 CLI 가 --dry-run 을 지원한다고 도움말에 나올 때만 --dry-run 으로 미리보기한다.
#
# 사용법 (WSL2 Ubuntu):
#   bash scripts/nemoclaw_probe.sh            # 샌드박스 자동 탐지
#   bash scripts/nemoclaw_probe.sh my-sandbox # 특정 샌드박스
#   OUT=/some/dir bash scripts/nemoclaw_probe.sh
#
# 결과는 OUT 폴더에 명령별 파일 + summary.tsv 로 저장된다. 기본 OUT 은 Windows 사용자 폴더의
# nemoclaw-probe (WSL 에서 cmd.exe 로 탐지), 실패하면 ~/nemoclaw-probe.
# 출력의 토큰·키·이메일·긴 16진수/base64 문자열은 저장 전에 마스킹된다. 그래도 공유 전에 한 번 훑어볼 것.
set -u

# 동시에 두 번 돌면 NemoClaw 호스트 잠금(~/.nemoclaw-portable-host.lock)을 서로 기다리다 전부 실패한다.
exec 9>"${TMPDIR:-/tmp}/nemoclaw_probe.lock"
if ! flock -n 9; then
  echo "!! 다른 nemoclaw_probe 가 이미 실행 중입니다. 끝난 뒤 다시 실행하세요."; exit 3
fi

TS="$(date +%Y%m%d-%H%M%S)"
if [ -z "${OUT:-}" ]; then
  WINHOME=""
  if command -v cmd.exe >/dev/null 2>&1 && command -v wslpath >/dev/null 2>&1; then
    WINHOME="$(cd /mnt/c 2>/dev/null && cmd.exe /c 'echo %USERPROFILE%' 2>/dev/null | tr -d '\r')"
    [ -n "$WINHOME" ] && WINHOME="$(wslpath "$WINHOME" 2>/dev/null || true)"
  fi
  if [ -n "$WINHOME" ] && [ -d "$WINHOME" ]; then OUT="$WINHOME/nemoclaw-probe/$TS"; else OUT="$HOME/nemoclaw-probe/$TS"; fi
fi
mkdir -p "$OUT" || { echo "cannot create $OUT"; exit 2; }
SUMMARY="$OUT/summary.tsv"
printf 'id\trc\tbytes\tcommand\n' > "$SUMMARY"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TIMEOUT_S="${TIMEOUT_S:-45}"

mask() {
  sed -E \
    -e 's/([Bb]earer )[A-Za-z0-9._~+/-]{8,}=*/\1[MASKED]/g' \
    -e 's/(sk-|lsv2_|nvapi-|ghp_|gho_|github_pat_|xox[bpas]-)[A-Za-z0-9_-]{8,}/\1[MASKED]/g' \
    -e 's/([Bb]earer|[Tt]oken|[Aa]pi[_-]?[Kk]ey|[Ss]ecret|[Pp]assword)([" :=]+)[^" ,}]{6,}/\1\2[MASKED]/g' \
    -e 's/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/[EMAIL]/g' \
    -e 's/\b[0-9a-fA-F]{40,}\b/[HEX]/g' \
    -e 's/[A-Za-z0-9+\/]{48,}={0,2}/[B64]/g'
}

# run <id> <command...>: 셸 해석 없이 인자 그대로 실행, 타임아웃, 마스킹 후 저장
run() {
  local id="$1"; shift
  local f="$OUT/$id.txt"
  { printf '$'; printf ' %q' "$@"; printf '\n\n'; } > "$f"
  timeout "$TIMEOUT_S" "$@" < /dev/null 2>&1 | head -c 2000000 | mask >> "$f"
  local rc=${PIPESTATUS[0]}
  printf '\n[exit %s]\n' "$rc" >> "$f"
  printf '%s\t%s\t%s\t%s\n' "$id" "$rc" "$(wc -c < "$f")" "$*" >> "$SUMMARY"
  printf '%-34s rc=%s\n' "$id" "$rc"
  if grep -q 'Failed to acquire lock' "$f"; then
    echo "!! NemoClaw 호스트 잠금을 다른 nemoclaw 프로세스가 쥐고 있습니다. 여기서 멈춥니다."
    echo "   확인: ps -eo pid,etimes,args | grep -E 'nemoclaw|openshell' | grep -v grep"
    printf 'aborted\tlock_contention\n' >> "$SUMMARY"
    echo "== 중단: $OUT"; exit 4
  fi
  return 0
}

echo "== 결과 폴더: $OUT"

echo "== 1. 호스트"
run 00-uname uname -a
run 01-os-release cat /etc/os-release
[ -r /proc/version ] && run 02-proc-version cat /proc/version
run 03-docker-version docker version --format '{{.Client.Version}} / server {{.Server.Version}}'
run 04-docker-ps docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}'
for b in nemoclaw openshell node npm python3 garak; do
  run "05-which-$b" bash -c "command -v $b && readlink -f \"\$(command -v $b)\""
done

if ! command -v nemoclaw >/dev/null 2>&1; then
  echo "!! nemoclaw 가 PATH 에 없습니다. 설치한 셸(예: nvm 을 쓰는 로그인 셸)에서 다시 실행하세요."
  echo "== 완료 (부분): $OUT"; exit 1
fi

echo "== 2. 버전과 도움말"
run 10-nemoclaw-version nemoclaw --version
run 11-nemoclaw-help nemoclaw --help
command -v openshell >/dev/null 2>&1 && run 12-openshell-version openshell --version
command -v openshell >/dev/null 2>&1 && run 13-openshell-help openshell --help
command -v openshell >/dev/null 2>&1 && run 14-openshell-policy-help openshell policy --help
command -v openshell >/dev/null 2>&1 && run 15-openshell-policy-get-help openshell policy get --help
run 16-nemoclaw-doctor nemoclaw doctor
run 17-nemoclaw-list nemoclaw list
run 18-nemoclaw-status nemoclaw status

echo "== 3. 샌드박스"
SB="${1:-}"
if [ -z "$SB" ]; then
  # list 출력에서 이름 규칙(1~19자, 소문자 시작)에 맞는 첫 단어 후보를 고른다
  SB="$(grep -oE '^[[:space:]]*[*>]?[[:space:]]*[a-z][a-z0-9-]{0,17}[a-z0-9]?' "$OUT/17-nemoclaw-list.txt" \
        | sed -E 's/^[[:space:]*>]+//' | grep -vxE 'nemoclaw|list|name|sandbox|sandboxes|no|exit|status' | head -1)"
fi
if [ -z "$SB" ]; then
  echo "!! 샌드박스를 찾지 못했습니다 (onboard 전일 수 있음). 17-nemoclaw-list.txt 를 확인하세요."
  printf 'sandbox\t(none)\n' >> "$SUMMARY"
else
  echo "   대상 샌드박스: $SB"
  printf 'sandbox\t%s\n' "$SB" >> "$SUMMARY"
  run 20-sb-status        nemoclaw "$SB" status
  run 21-sb-status-json   nemoclaw "$SB" status --json
  run 22-sb-doctor-json   nemoclaw "$SB" doctor --json
  run 23-sb-policy-list   nemoclaw "$SB" policy list
  run 24-sb-policy-get    nemoclaw "$SB" policy get
  run 25-sb-policy-help   nemoclaw "$SB" policy --help
  run 26-sb-policy-add-help nemoclaw "$SB" policy add --help
  run 27-sb-exec-help     nemoclaw "$SB" exec --help
  run 28-sb-help          nemoclaw "$SB" --help
  command -v openshell >/dev/null 2>&1 && run 29-openshell-policy-get-base openshell policy get --base "$SB"
  # 샌드박스 내부 실행 경로 확인 — 무해한 명령만
  run 30-sb-exec-true     nemoclaw "$SB" exec -- true
  run 31-sb-exec-id       nemoclaw "$SB" exec -- id
  run 32-sb-exec-python   nemoclaw "$SB" exec -- python3 --version
  if grep -q -- '--dry-run' "$OUT/26-sb-policy-add-help.txt" 2>/dev/null; then
    for p in "$REPO_DIR"/nemoclaw/presets/*.yaml; do
      [ -f "$p" ] && run "40-dryrun-$(basename "$p" .yaml)" nemoclaw "$SB" policy add --from-file "$p" --dry-run
    done
  else
    echo "   policy add 도움말에 --dry-run 이 없어 미리보기를 건너뜁니다 (적용은 하지 않음)."
  fi
fi

echo "== 완료: $OUT"
echo "   summary.tsv 와 *.txt 를 확인한 뒤, 이 폴더를 Claude 에 연결하거나 내용을 붙여 주세요."
