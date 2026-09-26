#!/usr/bin/env bash
# Run every --self-test in the repository and exit nonzero if any fails.
#
# A script opts in by handling --self-test; this finds them by that string, so a
# new hook or script is covered without editing this file. Each one gets 300
# seconds. Scripts without a self-test are listed, so a new one that skipped its
# test shows up here. The plugin's JSON files are also checked for duplicate keys:
# a text merge can leave two "PostToolUse" keys, which still parses, and the
# parser silently keeps only the last. Run it from anywhere: tests/run-all.sh
set -uo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root" || exit 2

mapfile -t scripts < <(grep -l -- '--self-test' bin/* hooks/*.py lib/*.py skills/*/*.sh skills/*/*.py 2>/dev/null | sort -u)
if [ "${#scripts[@]}" -eq 0 ]; then
  echo "run-all: found no scripts with a --self-test" >&2
  exit 1
fi

failed=0
if out=$(python3 - .claude-plugin/*.json hooks/hooks.json 2>&1 <<'PY'
import json, sys
def no_dupes(pairs):
    keys = [k for k, _ in pairs]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    if dupes:
        raise ValueError("duplicate keys: " + ", ".join(dupes))
    return dict(pairs)
for path in sys.argv[1:]:
    try:
        json.load(open(path), object_pairs_hook=no_dupes)
    except ValueError as e:
        sys.exit(f"{path}: {e}")
PY
); then
  echo "ok   plugin json has no duplicate keys"
else
  echo "FAIL plugin json"
  printf '%s\n' "$out" | sed 's/^/     /'
  failed=$((failed + 1))
fi

# Claude Code updates an installed plugin only when its version changes, so a change to what
# installs, made after the current version was tagged, never reaches anyone until the version moves.
version=$(python3 -c 'import json; print(json.load(open(".claude-plugin/plugin.json"))["version"])')
if git rev-parse -q --verify "refs/tags/v$version" >/dev/null; then
  if git diff --quiet "v$version" HEAD -- skills hooks bin lib .claude-plugin; then
    echo "ok   version $version matches its tag"
  else
    echo "FAIL skills, hooks, bin or lib changed since v$version: bump the version in .claude-plugin/plugin.json"
    failed=$((failed + 1))
  fi
else
  echo "ok   version $version is not tagged yet; tag v$version when it merges"
fi

missing=$(grep -L -- '--self-test' bin/* hooks/*.py 2>/dev/null | xargs -r -n1 basename | tr '\n' ' ')
[ -n "$missing" ] && echo "no self-test: $missing"

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
