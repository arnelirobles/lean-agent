#!/usr/bin/env bash
# Run every --self-test in the repository and exit nonzero if any fails.
#
# A script opts in by handling --self-test; this finds them by that string, so a
# new hook or script is covered without editing this file. Each one gets 300
# seconds. Run it from anywhere: tests/run-all.sh
set -uo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root" || exit 2

mapfile -t scripts < <(grep -l -- '--self-test' bin/* hooks/*.py lib/*.py skills/*/*.sh skills/*/*.py 2>/dev/null | sort -u)
if [ "${#scripts[@]}" -eq 0 ]; then
  echo "run-all: found no scripts with a --self-test" >&2
  exit 1
fi

failed=0
for script in "${scripts[@]}"; do
  case "$script" in
    *.py) runner=(python3 "$script") ;;
    *.sh) runner=(bash "$script") ;;
    *) runner=("./$script") ;;
  esac
  if out=$(timeout 300 "${runner[@]}" --self-test 2>&1); then
    echo "ok   $script"
  else
    echo "FAIL $script"
    printf '%s\n' "$out" | sed 's/^/     /'
    failed=$((failed + 1))
  fi
done

echo "run-all: ${#scripts[@]} self-tests, $failed failed"
[ "$failed" -eq 0 ]
