#!/usr/bin/env bash
# nemoclaw 샌드박스 명령이 멈추는 지점을 진단한다 — 읽기 전용.
# 명령을 뒤에서 실행하고 WAIT 초 뒤 연결·대기 상태와 Node 네트워크 디버그 로그를 저장한 다음 그 프로세스만 종료한다.
#
# 사용법 (WSL): bash -l scripts/nemoclaw_hang_diag.sh [샌드박스] [policy-list|status|exec-true]
#   WAIT=20  OUT=<폴더>  로 조정 가능. 기본 OUT 은 Windows 사용자 폴더의 nemoclaw-probe/diag-<시각>.
set -u
SB="${1:-my-assistant}"
CASE="${2:-policy-list}"
WAIT="${WAIT:-20}"
case "$CASE" in
  policy-list) CMD=(nemoclaw "$SB" policy list) ;;
  status)      CMD=(nemoclaw "$SB" status) ;;
  exec-true)   CMD=(nemoclaw "$SB" exec -- true) ;;
  *) echo "unknown case: $CASE"; exit 2 ;;
esac

TS="$(date +%Y%m%d-%H%M%S)"
if [ -z "${OUT:-}" ]; then
  WINHOME="$(cd /mnt/c 2>/dev/null && cmd.exe /c 'echo %USERPROFILE%' 2>/dev/null | tr -d '\r')"
  [ -n "$WINHOME" ] && WINHOME="$(wslpath "$WINHOME" 2>/dev/null || true)"
  if [ -n "$WINHOME" ] && [ -d "$WINHOME" ]; then OUT="$WINHOME/nemoclaw-probe/diag-$TS"; else OUT="$HOME/nemoclaw-probe/diag-$TS"; fi
fi
mkdir -p "$OUT"
R="$OUT/report.txt"

mask() {
  sed -E \
    -e 's/([Bb]earer )[A-Za-z0-9._~+/-]{8,}=*/\1[MASKED]/g' \
    -e 's/(sk-|lsv2_|nvapi-|ghp_|github_pat_)[A-Za-z0-9_-]{8,}/\1[MASKED]/g' \
    -e 's/([Bb]earer|[Tt]oken|[Aa]uthorization|[Aa]pi[_-]?[Kk]ey|[Ss]ecret|[Pp]assword)([" :=]+)[^" ,}]{6,}/\1\2[MASKED]/g' \
    -e 's/[A-Za-z0-9+\/]{48,}={0,2}/[B64]/g'
}

{
  echo "case=$CASE sandbox=$SB wait=${WAIT}s  $(date -Is)"
  echo "== 실행 전 nemoclaw 프로세스 (남아 있으면 잠금을 쥐고 있을 수 있음)"
  ps -eo pid,etimes,stat,args | grep -E '[n]emoclaw|[o]penshell'
  echo "== 실행 전 8080 연결"
  ss -tnp state established '( sport = :8080 or dport = :8080 )' 2>/dev/null
} > "$R"

NODE_DEBUG=net,http,http2,tls,fetch "${CMD[@]}" </dev/null >"$OUT/cmd.out" 2>"$OUT/cmd.err" &
P=$!
sleep "$WAIT"

{
  echo
  echo "== ${WAIT}초 뒤 (pid=$P)"
  if kill -0 "$P" 2>/dev/null; then
    echo "state: still running"
    echo "wchan: $(cat /proc/$P/wchan 2>/dev/null)"
    echo "threads: $(ls /proc/$P/task 2>/dev/null | wc -l)  sockets: $(ls -l /proc/$P/fd 2>/dev/null | grep -c socket)"
    echo "-- 자식 프로세스"
    ps -eo pid,ppid,etimes,stat,args --forest | awk -v p="$P" '$1==p||$2==p'
    echo "-- 이 프로세스의 TCP 연결"
    ss -tnpa 2>/dev/null | grep "pid=$P,"
    echo "-- 8080 전체 연결"
    ss -tnp state established '( sport = :8080 or dport = :8080 )' 2>/dev/null
  else
    wait "$P"; echo "state: exited rc=$?"
  fi
  echo
  echo "== stdout (마지막 20줄)"; tail -20 "$OUT/cmd.out"
  echo "== stderr / NODE_DEBUG (마지막 60줄)"; grep -v ExperimentalWarning "$OUT/cmd.err" | tail -60
} 2>&1 | mask >> "$R"

if kill -0 "$P" 2>/dev/null; then
  kill "$P" 2>/dev/null; sleep 1; kill -9 "$P" 2>/dev/null
  echo "(진단 후 pid $P 종료)" >> "$R"
fi
mask < "$OUT/cmd.err" > "$OUT/cmd.err.masked" && rm -f "$OUT/cmd.err"
echo "== 완료: $OUT"
