#!/usr/bin/env bash
# Runs --self-test on every script in this plugin's bin/ and hooks/ that has one, and fails if any
# fails or if none ran.
#
#   self-test-all.sh
#   self-test-all.sh --self-test
#
# A script counts as having a self-test when its source mentions --self-test. Scripts without one
# are listed, so a new script that skipped its test shows up here rather than nowhere. Each test
# runs with a 120 second deadline, so one hung test cannot hold the whole run past a tool timeout.

set -uo pipefail

run_all() { # directories to scan
  local f ran=0 failed=0 missing="" out rc
  for f in $(find "$@" -maxdepth 1 -type f \( -name '*.sh' -o -name '*.py' \) 2>/dev/null | sort); do
    [ "$(basename "$f")" = "self-test-all.sh" ] && continue
    if ! grep -q -- '--self-test' "$f"; then
      missing="$missing $(basename "$f")"
      continue
    fi
    ran=$((ran + 1))
    case "$f" in *.py) out=$(timeout 120 python3 "$f" --self-test 2>&1) ;; *) out=$(timeout 120 bash "$f" --self-test 2>&1) ;; esac
    rc=$?
    if [ "$rc" = 0 ]; then
      echo "  pass  $(basename "$f")"
    else
      echo "  FAIL  $(basename "$f") (exit $rc)"
      printf '%s\n' "$out" | tail -15 | sed 's/^/        /'
      failed=$((failed + 1))
    fi
  done
  [ -n "$missing" ] && echo "  no self-test:$missing"
  if [ "$ran" = 0 ]; then
    echo "self-test-all: no self-tests found in $*, so nothing was tested"
    return 1
  fi
  echo "self-test-all: $ran ran, $failed failed"
  [ "$failed" = 0 ]
}

if [ "${1:-}" = "--self-test" ]; then
  t=$(mktemp -d); trap 'rm -rf "$t"' EXIT
  fails=0
  printf '#!/bin/sh\n[ "$1" = --self-test ] && exit 0\n' > "$t/good.sh"
  run_all "$t" >/dev/null || { echo "self-test failed: a passing script failed the run"; fails=$((fails + 1)); }
  printf '#!/bin/sh\n[ "$1" = --self-test ] && exit 1\n' > "$t/bad.sh"
  run_all "$t" >/dev/null && { echo "self-test failed: a failing script passed the run"; fails=$((fails + 1)); }
  rm "$t/good.sh" "$t/bad.sh"; echo 'echo hi' > "$t/plain.sh"
  run_all "$t" >/dev/null && { echo "self-test failed: a run with no self-tests passed"; fails=$((fails + 1)); }
  [ "$fails" = 0 ] && echo "self-test passed" && exit 0
  exit 1
fi

root=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
run_all "$root/bin" "$root/hooks"
