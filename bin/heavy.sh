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
# the lane is busy, the holders are printed once, from /proc/*/fd (only processes you can see, so
# normally your own user's). A holder that is not a build at all is how a stuck lane is diagnosed:
# a compiler server that inherited the lock looked exactly like a slow build for ten minutes.
#
#   heavy.sh --self-test

set -uo pipefail

lock_holders() { # $1 lock path; prints each visible process with it open, and what that process runs
  local lock fd pid kid
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

run_lane() { # $1 lane, rest: command
  local lane=$1 dir lock wait marker holders rc
  shift
  case "$lane" in
    *[!A-Za-z0-9._-]*) echo "lane '$lane' must be letters, digits, dot, dash or underscore" >&2; return 2 ;;
  esac
  wait=${HEAVY_WAIT:-300}
  case "$wait" in ''|*[!0-9]*) echo "HEAVY_WAIT must be a whole number of seconds" >&2; return 2 ;; esac
  dir=${HEAVY_LOCK_DIR:-/tmp/heavy-locks}
  mkdir -p "$dir"
  lock="$dir/$lane.lock"
  : >> "$lock"

  if ! flock -n "$lock" true 2>/dev/null; then
    holders=$(lock_holders "$lock")
    echo "heavy.sh: lane '$lane' is busy, waiting up to ${wait}s. Held by:" >&2
    printf '%s\n' "${holders:-  (no visible process; another user may hold it)}" >&2
  fi

  # -o closes the lock before the command runs. Without it every child the command starts inherits
  # the lock, and a child that outlives the build keeps it. The Roslyn compiler server does exactly
  # that: it stays up for minutes after `dotnet build` exits, so the lane stayed locked with nothing
  # building and every other agent waited on it. The lock still covers the whole command, because
  # flock keeps its own copy until the command exits.
  #
  # The marker tells a timeout (flock's 75) from a command that itself exits 75. flock runs in the
  # foreground so stdin reaches the command and a Ctrl-C reaches both, never freeing the lock while
  # the command runs on.
  marker=$(mktemp)
  rm -f "$marker"
  flock -o -w "$wait" -E 75 "$lock" sh -c ': > "$0"; exec nice -n 19 "$@"' "$marker" "$@"
  rc=$?
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

self_test() {
  local fails=0 holder out rc i
  t=$(mktemp -d)
  holder=""
  trap 'rm -rf "$t"; [ -n "${holder:-}" ] && kill "$holder" 2>/dev/null' EXIT
  export HEAVY_LOCK_DIR="$t/locks"
  out=$( (run_lane l1 sh -c 'exit 3') 2>&1 ); rc=$?
  [ "$rc" = 3 ] || { echo "self-test failed: command exit code not passed through ($rc)"; fails=$((fails + 1)); }
  out=$( (run_lane l1 sh -c 'exit 75') 2>&1 ); rc=$?
  { [ "$rc" = 75 ] && ! grep -q 'timed out' <<<"$out"; } || { echo "self-test failed: a command exiting 75 read as a timeout: $out"; fails=$((fails + 1)); }
  out=$( (run_lane 'bad/lane' true) 2>&1 ); [ $? = 2 ] || { echo "self-test failed: bad lane name accepted"; fails=$((fails + 1)); }
  # Through the script itself, as a caller runs it: a backgrounded flock in a script gets /dev/null.
  out=$(printf 'through stdin\n' | bash "$(readlink -f "$0")" l1 cat 2>&1)
  [ "$out" = "through stdin" ] || { echo "self-test failed: stdin did not reach the command: '$out'"; fails=$((fails + 1)); }

  # Hold the lane the way heavy.sh does, so the holder is flock and the work is its child.
  (run_lane l2 sleep 7 </dev/null >/dev/null 2>&1) &
  holder=$!
  for i in $(seq 1 50); do flock -n "$HEAVY_LOCK_DIR/l2.lock" true 2>/dev/null || break; sleep 0.1; done
  out=$(lock_holders "$HEAVY_LOCK_DIR/l2.lock")
  grep -q 'running pid [0-9]*: sleep 7' <<<"$out" || { echo "self-test failed: the holder's command not listed: '$out'"; fails=$((fails + 1)); }
  out=$( (HEAVY_WAIT=1 run_lane l2 touch "$t/ran") 2>&1 ); rc=$?
  [ "$rc" = 75 ] || { echo "self-test failed: timeout exit was $rc, want 75"; fails=$((fails + 1)); }
  grep -q "timed out after 1s waiting for lane 'l2'" <<<"$out" || { echo "self-test failed: no timeout message: $out"; fails=$((fails + 1)); }
  { [ "$(grep -c 'Held by' <<<"$out")" = 2 ] && grep -q 'running pid [0-9]*: sleep 7' <<<"$out"; } || { echo "self-test failed: holders not printed once while waiting and once at timeout: $out"; fails=$((fails + 1)); }
  [ ! -e "$t/ran" ] || { echo "self-test failed: the command ran without the lock"; fails=$((fails + 1)); }
  out=$( (HEAVY_WAIT=15 run_lane l2 touch "$t/ran") 2>&1 ); rc=$?
  { [ "$rc" = 0 ] && [ -e "$t/ran" ]; } || { echo "self-test failed: lane did not run once free: $rc $out"; fails=$((fails + 1)); }
  wait "$holder" 2>/dev/null; holder=""
  [ "$fails" = 0 ] && echo "self-test passed" && return 0
  return 1
}

if [ "${1:-}" = "--self-test" ]; then self_test; exit $?; fi

if [ $# -lt 2 ]; then
  echo "usage: heavy.sh <lane> <command> [args...]" >&2
  echo "no command given, so nothing would run and this would report success." >&2
  exit 2
fi

run_lane "$@"
