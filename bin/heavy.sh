#!/usr/bin/env bash
# Runs a heavy command in a lane that every agent on the machine shares, one command per lane.
#
#   bash heavy.sh <lane> <command> [args...]
#
#   bash heavy.sh dotnet dotnet build MySolution.sln -nodeReuse:false
#   bash heavy.sh node   npm ci
#   bash heavy.sh node   npx playwright test e2e/login.spec.ts
#
# Why this exists. Thirteen agents on a three core machine each ran a reasonable brief, and five of
# them compiled the same solution at once. Load average reached 44 on a box that also serves live
# sites. Reading and writing code is cheap; compiling, installing packages, starting containers and
# driving browsers is what saturates a machine. So the agents all keep running and only those
# commands queue.
#
# A lane is just a name. Use one per toolchain, so a .NET build and an npm install can run side by
# side but two builds cannot. Everything runs at the lowest CPU priority, so a live service on the
# same machine wins every contest.
#
# HEAVY_LOCK_DIR sets where the lock files live. Every agent has to agree on it, which is the point
# of the default being one fixed path rather than a per-session temp dir.
#
# Given no command it fails rather than taking the lock and exiting 0. A queue step that succeeds
# having run nothing is the failure section 10 is about.
#
# The wait has a deadline: HEAVY_WAIT seconds (default 300), then exit 75 naming the lock and the
# processes holding it. An agent's tool call is cut off at 600 seconds, and a cut-off call becomes a
# background job the agent is never told about, so a wait with no deadline is a stalled agent. When
# the lane is busy, the holders are printed once, from /proc/*/fd on Linux or lsof on macOS (only
# processes you can see, so normally your own user's). A holder that is not a build at all is how a stuck lane is diagnosed:
# a compiler server that inherited the lock looked exactly like a slow build for ten minutes.
#
#   heavy.sh status <lane>      prints free, or busy and the holders. Exit 0 free, 1 busy.
#   heavy.sh --self-test
#
# Use status instead of a pgrep loop. `while pgrep -f "dotnet test"` matches the shell running the
# loop, because its own command line contains the string, and never ends.
#
# Stall watchdog. HEAVY_STALL=<seconds> stops a command whose output shows no new progress for that
# long, and exits 76 with "stalled: no test progress for N s; likely load, not code". Progress is a
# line matching HEAVY_PROGRESS (a Python regex; the default matches dotnet's
# "[+N/xM/?K]" counter and lines starting passed or failed) whose matched text differs from the last
# progress line, so a counter that repeats with the same counts does not count. The clock starts when
# the command starts, so set it above the build time. Only the command heavy.sh started is stopped.
#
# HEAVY_TIMING_FILE=<path> gets the seconds the command waited for the lane, once it has run.
#
# Linux uses util-linux flock. Anywhere else (macOS has no flock and no /proc) the lock is taken with
# python3 fcntl.flock, in lib/heavy_lock.py, and holders are named with lsof. The watchdog also runs
# through that helper. HEAVY_BACKEND=python forces it on Linux, for the self-test.

set -uo pipefail

here() { # the directory this script really lives in, through the root heavy.sh symlink
  local src=${BASH_SOURCE[0]} dir
  while [ -L "$src" ]; do
    dir=$(cd -P "$(dirname "$src")" && pwd)
    src=$(readlink "$src")
    case "$src" in /*) ;; *) src="$dir/$src" ;; esac
  done
  cd -P "$(dirname "$src")" && pwd
}
HEAVY_HELPER="$(here)/../lib/heavy_lock.py"
HEAVY_SELF="$(here)/$(basename "${BASH_SOURCE[0]}")"

backend() { # flock on Linux with util-linux, the python helper everywhere else or when watching
  if [ "${HEAVY_BACKEND:-}" = python ] || [ -n "${HEAVY_STALL:-}" ] \
     || [ "$(uname -s)" != Linux ] || ! command -v flock >/dev/null 2>&1; then
    echo python
  else
    echo flock
  fi
}

lock_free() { # $1 lock path
  if [ "$(backend)" = flock ]; then flock -n "$1" true 2>/dev/null
  else python3 "$HEAVY_HELPER" probe "$1"; fi
}

lock_holders() { # $1 lock path; prints each visible process with it open, and what that process runs
  local lock fd pid kid
  if [ ! -d /proc/self ]; then
    lock="$1"
    for pid in $(lsof -t -- "$lock" 2>/dev/null | sort -u); do
      [ "$pid" = "$$" ] && continue
      printf '  pid %s: %s\n' "$pid" "$(ps -o command= -p "$pid" 2>/dev/null | cut -c1-150)"
      for kid in $(pgrep -P "$pid" 2>/dev/null); do
        printf '    running pid %s: %s\n' "$kid" "$(ps -o command= -p "$kid" 2>/dev/null | cut -c1-150)"
      done
    done
    return 0
  fi
  lock=$(readlink -f "$1")
  for fd in /proc/[0-9]*/fd/*; do
    [ "$(readlink "$fd" 2>/dev/null)" = "$lock" ] || continue
    pid=${fd#/proc/}; pid=${pid%%/*}
    printf '  pid %s: %s\n' "$pid" "$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | cut -c1-150)"
    # The holder is usually flock itself; the command it runs is its child, and that is the part
    # that says whether the lane is doing real work.
    for kid in $(cat /proc/"$pid"/task/*/children 2>/dev/null); do
      printf '    running pid %s: %s\n' "$kid" "$(tr '\0' ' ' < "/proc/$kid/cmdline" 2>/dev/null | cut -c1-150)"
    done
  done 2>/dev/null | awk '!seen[$0]++'
}

check_lane() { # $1 lane
  case "$1" in
    *[!A-Za-z0-9._-]*|'') echo "lane '$1' must be letters, digits, dot, dash or underscore" >&2; return 2 ;;
  esac
}

lane_status() { # $1 lane: free (exit 0) or busy with the holders (exit 1)
  local lane=$1 dir lock holders
  check_lane "$lane" || return 2
  dir=${HEAVY_LOCK_DIR:-/tmp/heavy-locks}
  lock="$dir/$lane.lock"
  if [ ! -e "$lock" ] || lock_free "$lock"; then
    echo "free"
    return 0
  fi
  holders=$(lock_holders "$lock")
  echo "busy: lane '$lane' held by:"
  printf '%s\n' "${holders:-  (no visible process; another user may hold it)}"
  return 1
}

run_lane() { # $1 lane, rest: command
  local lane=$1 dir lock wait marker holders rc t0
  shift
  check_lane "$lane" || return 2
  wait=${HEAVY_WAIT:-300}
  case "$wait" in ''|*[!0-9]*) echo "HEAVY_WAIT must be a whole number of seconds" >&2; return 2 ;; esac
  case "${HEAVY_STALL:-0}" in ''|*[!0-9]*) echo "HEAVY_STALL must be a whole number of seconds" >&2; return 2 ;; esac
  dir=${HEAVY_LOCK_DIR:-/tmp/heavy-locks}
  mkdir -p "$dir"
  lock="$dir/$lane.lock"
  : >> "$lock"

  if ! lock_free "$lock"; then
    holders=$(lock_holders "$lock")
    echo "heavy.sh: lane '$lane' is busy, waiting up to ${wait}s. Held by:" >&2
    printf '%s\n' "${holders:-  (no visible process; another user may hold it)}" >&2
  fi

  # -o closes the lock before the command runs. Without it every child the command starts inherits
  # the lock, and a child that outlives the build keeps it. The Roslyn compiler server does exactly
  # that: it stays up for minutes after `dotnet build` exits, so the lane stayed locked with nothing
  # building and every other agent waited on it. The lock still covers the whole command, because
  # flock keeps its own copy until the command exits. The python helper's lock fd is close-on-exec,
  # which does the same.
  #
  # The marker tells a timeout (flock's 75) from a command that itself exits 75. flock runs in the
  # foreground so stdin reaches the command and a Ctrl-C reaches both, never freeing the lock while
  # the command runs on.
  marker=$(mktemp)
  rm -f "$marker"
  t0=$(date +%s)
  if [ "$(backend)" = flock ]; then
    flock -o -w "$wait" -E 75 "$lock" sh -c ': > "$0"; exec nice -n 19 "$@"' "$marker" "$@"
  else
    python3 "$HEAVY_HELPER" run "$lock" "$wait" "$marker" "$@"
  fi
  rc=$?
  # HEAVY_TIMING_FILE gets the seconds spent waiting for the lane, for callers that keep statistics.
  if [ -n "${HEAVY_TIMING_FILE:-}" ] && [ -e "$marker" ]; then
    python3 -c 'import os,sys; print(max(0, int(os.stat(sys.argv[1]).st_mtime) - int(sys.argv[2])))' "$marker" "$t0" > "$HEAVY_TIMING_FILE" 2>/dev/null
  fi
  if [ "$rc" = 75 ] && [ ! -e "$marker" ]; then
    holders=$(lock_holders "$lock")
    echo "heavy.sh: timed out after ${wait}s waiting for lane '$lane' ($lock). Held by:" >&2
    printf '%s\n' "${holders:-  (no visible process; another user may hold it)}" >&2
    echo "  Nothing ran. If the holder is not a build (a compiler server, a stuck shell), that is the" >&2
    echo "  problem; otherwise run again later or raise HEAVY_WAIT, keeping it under your tool timeout." >&2
  fi
  rm -f "$marker"
  return "$rc"
}

self_suite() { # runs the lane tests with the current HEAVY_BACKEND; prints failures, returns their count
  local fails=0 holder out rc i hold=7
  holder=""
  # lsof takes a couple of seconds a call, so the holder has to outlast several of them.
  [ -d /proc/self ] || hold=25
  export HEAVY_LOCK_DIR="$t/locks"
  out=$( (run_lane l1 sh -c 'exit 3') 2>&1 ); rc=$?
  [ "$rc" = 3 ] || { echo "self-test failed: command exit code not passed through ($rc)"; fails=$((fails + 1)); }
  out=$( (run_lane l1 sh -c 'exit 75') 2>&1 ); rc=$?
  { [ "$rc" = 75 ] && ! grep -q 'timed out' <<<"$out"; } || { echo "self-test failed: a command exiting 75 read as a timeout: $out"; fails=$((fails + 1)); }
  out=$( (run_lane 'bad/lane' true) 2>&1 ); [ $? = 2 ] || { echo "self-test failed: bad lane name accepted"; fails=$((fails + 1)); }
  # Through the script itself, as a caller runs it: a backgrounded flock in a script gets /dev/null.
  out=$(printf 'through stdin\n' | bash "$HEAVY_SELF" l1 cat 2>&1)
  [ "$out" = "through stdin" ] || { echo "self-test failed: stdin did not reach the command: '$out'"; fails=$((fails + 1)); }
  out=$(bash "$HEAVY_SELF" status l1); rc=$?
  { [ "$rc" = 0 ] && [ "$out" = free ]; } || { echo "self-test failed: idle lane did not read free: $rc '$out'"; fails=$((fails + 1)); }

  # Hold the lane the way heavy.sh does, so the holder is the lock process and the work is its child.
  (run_lane l2 sleep $hold </dev/null >/dev/null 2>&1) &
  holder=$!
  # Poll for the holder itself, not just a busy lock: run_lane's own probe holds the lock for an
  # instant before the real lock is taken, and a check that landed there found no holder (CI).
  for i in $(seq 1 50); do
    out=$(lock_holders "$HEAVY_LOCK_DIR/l2.lock")
    grep -q "running pid [0-9]*: .*sleep $hold" <<<"$out" && break
    sleep 0.1
  done
  grep -q "running pid [0-9]*: .*sleep $hold" <<<"$out" || { echo "self-test failed: the holder's command not listed: '$out'"; fails=$((fails + 1)); }
  out=$(bash "$HEAVY_SELF" status l2); rc=$?
  { [ "$rc" = 1 ] && grep -q '^busy' <<<"$out" && grep -q "sleep $hold" <<<"$out"; } || { echo "self-test failed: busy lane did not read busy with its holder: $rc '$out'"; fails=$((fails + 1)); }
  # Linux runs this suite twice, so clear the file the first pass left.
  rm -f "$t/ran"
  out=$( (HEAVY_WAIT=1 run_lane l2 touch "$t/ran") 2>&1 ); rc=$?
  [ "$rc" = 75 ] || { echo "self-test failed: timeout exit was $rc, want 75"; fails=$((fails + 1)); }
  grep -q "timed out after 1s waiting for lane 'l2'" <<<"$out" || { echo "self-test failed: no timeout message: $out"; fails=$((fails + 1)); }
  { [ "$(grep -c 'Held by' <<<"$out")" = 2 ] && grep -q "running pid [0-9]*: .*sleep $hold" <<<"$out"; } || { echo "self-test failed: holders not printed once while waiting and once at timeout: $out"; fails=$((fails + 1)); }
  [ ! -e "$t/ran" ] || { echo "self-test failed: the command ran without the lock"; fails=$((fails + 1)); }
  out=$( (HEAVY_WAIT=$((hold + 10)) run_lane l2 touch "$t/ran") 2>&1 ); rc=$?
  { [ "$rc" = 0 ] && [ -e "$t/ran" ]; } || { echo "self-test failed: lane did not run once free: $rc $out"; fails=$((fails + 1)); }
  wait "$holder" 2>/dev/null
  out=$(bash "$HEAVY_SELF" status l2); rc=$?
  [ "$rc" = 0 ] || { echo "self-test failed: lane still busy after the holder exited: $out"; fails=$((fails + 1)); }
  # The wait is reported to HEAVY_TIMING_FILE.
  (HEAVY_TIMING_FILE="$t/timing" run_lane l4 true) >/dev/null 2>&1
  [ -s "$t/timing" ] && [ "$(cat "$t/timing")" -ge 0 ] 2>/dev/null || { echo "self-test failed: HEAVY_TIMING_FILE not written"; fails=$((fails + 1)); }
  # The command runs at the lowest priority.
  out=$( (run_lane l3 sh -c 'ps -o nice= -p $$') 2>&1 )
  # nice adds to the caller's niceness, so a caller that is already niced can land on 20.
  case "$(echo $out)" in 19|20) ;; *) false ;; esac || { echo "self-test failed: command not at nice 19: '$out'"; fails=$((fails + 1)); }
  return "$fails"
}

stall_suite() { # the watchdog, always through the python helper
  local fails=0 out rc start
  export HEAVY_LOCK_DIR="$t/locks"
  # A counter that repeats with the same counts is a stall, even though bytes keep arriving.
  start=$SECONDS
  out=$( (HEAVY_STALL=2 run_lane s1 sh -c 'echo "[+1/x0/?0] a"; while :; do echo "[+1/x0/?0] a (${SECONDS}s)"; sleep 0.3; done') 2>&1 ); rc=$?
  [ "$rc" = 76 ] || { echo "self-test failed: repeating counter exit was $rc, want 76"; fails=$((fails + 1)); }
  grep -q 'stalled: no test progress for 2 s; likely load, not code' <<<"$out" || { echo "self-test failed: no stall message: $out"; fails=$((fails + 1)); }
  [ $((SECONDS - start)) -lt 15 ] || { echo "self-test failed: stall took too long"; fails=$((fails + 1)); }
  # Counts that move keep it alive (generous margins: the box running this may be under heavy load), and the command's own exit code comes through.
  out=$( (HEAVY_STALL=25 run_lane s2 sh -c 'for i in 1 2 3 4 5 6; do echo "[+$i/x0/?0] t"; sleep 0.5; done; exit 4') 2>&1 ); rc=$?
  [ "$rc" = 4 ] || { echo "self-test failed: moving counter was cut off or lost its exit code ($rc): $out"; fails=$((fails + 1)); }
  # The real Microsoft Testing Platform line: blank lines between, a time that changes every few seconds, counts that do not.
  out=$( (HEAVY_STALL=2 run_lane s8 sh -c 'i=0; while :; do i=$((i + 3)); printf "\n[+6683/x36/?1] BarakoCMS.Tests.dll (net10.0|arm64)(2h 37m %02ds)\n" $i; sleep 0.3; done') 2>&1 ); rc=$?
  { [ "$rc" = 76 ] && grep -q 'stalled: no test progress for 2 s' <<<"$out"; } || { echo "self-test failed: MTP line with only the time changing was not a stall ($rc): $out"; fails=$((fails + 1)); }
  # The same line with the passed count moving is progress.
  out=$( (HEAVY_STALL=25 run_lane s9 sh -c 'for i in 1 2 3 4 5 6; do printf "\n[+%d/x0/?1] BarakoCMS.Tests.dll (net10.0|arm64)(%ds)\n" $((6680 + i)) $((i * 3)); sleep 0.5; done; exit 4') 2>&1 ); rc=$?
  [ "$rc" = 4 ] || { echo "self-test failed: MTP line with moving counts was cut off ($rc): $out"; fails=$((fails + 1)); }
  # Result lines are progress, and a custom pattern replaces the default.
  out=$( (HEAVY_STALL=25 run_lane s3 sh -c 'for i in 1 2 3 4; do echo "passed Test$i"; sleep 0.5; done') 2>&1 ); rc=$?
  [ "$rc" = 0 ] || { echo "self-test failed: passed lines not read as progress ($rc): $out"; fails=$((fails + 1)); }
  out=$( (HEAVY_STALL=25 HEAVY_PROGRESS='^tick [0-9]+' run_lane s4 sh -c 'for i in 1 2 3 4; do echo "tick $i"; sleep 0.5; done') 2>&1 ); rc=$?
  [ "$rc" = 0 ] || { echo "self-test failed: custom progress pattern ignored ($rc): $out"; fails=$((fails + 1)); }
  # Only the command's own group is stopped, a child included, and a bystander is left alone.
  sleep 30 & local bystander=$!
  out=$( (HEAVY_STALL=1 run_lane s5 sh -c 'sleep 40 & echo $! > "$0"; wait' "$t/child.pid") 2>&1 ); rc=$?
  [ "$rc" = 76 ] || { echo "self-test failed: silent command exit was $rc, want 76"; fails=$((fails + 1)); }
  if kill -0 "$(cat "$t/child.pid" 2>/dev/null)" 2>/dev/null; then
    echo "self-test failed: the command's child survived the stall"; fails=$((fails + 1)); kill "$(cat "$t/child.pid")" 2>/dev/null
  fi
  kill -0 "$bystander" 2>/dev/null || { echo "self-test failed: a process heavy.sh did not start was stopped"; fails=$((fails + 1)); }
  kill "$bystander" 2>/dev/null; wait "$bystander" 2>/dev/null
  out=$( (HEAVY_STALL=abc run_lane s6 true) 2>&1 ); rc=$?
  [ "$rc" = 2 ] || { echo "self-test failed: bad HEAVY_STALL accepted ($rc)"; fails=$((fails + 1)); }
  out=$( (HEAVY_STALL=60 run_lane s7 true) 2>&1 ); rc=$?
  [ "$rc" = 0 ] || { echo "self-test failed: quick command under the watchdog exited $rc: $out"; fails=$((fails + 1)); }
  return "$fails"
}

self_test() {
  local fails=0 n
  if ! command -v python3 >/dev/null 2>&1 && ! command -v flock >/dev/null 2>&1; then
    echo "needs python3 or flock"; return 77
  fi
  t=$(mktemp -d)
  trap 'rm -rf "$t"; jobs -p | xargs kill 2>/dev/null' EXIT
  self_suite; n=$?; fails=$((fails + n))
  if [ "$(backend)" = flock ]; then
    # Linux ran flock above; run the helper path too, since it is the one macOS depends on.
    HEAVY_BACKEND=python self_suite; n=$?; fails=$((fails + n))
  fi
  stall_suite; n=$?; fails=$((fails + n))
  [ "$fails" = 0 ] && echo "self-test passed" && return 0
  return 1
}

if [ "${1:-}" = "--self-test" ]; then self_test; exit $?; fi

if [ "$(backend)" = flock ] || command -v python3 >/dev/null 2>&1; then :; else
  echo "heavy.sh: neither flock nor python3 found, so there is no lane to queue in and nothing ran." >&2
  exit 69
fi

if [ "${1:-}" = "status" ] && [ $# -eq 2 ]; then lane_status "$2"; exit $?; fi

if [ $# -lt 2 ]; then
  echo "usage: heavy.sh <lane> <command> [args...]   |   heavy.sh status <lane>" >&2
  echo "no command given, so nothing would run and this would report success." >&2
  exit 2
fi

run_lane "$@"
