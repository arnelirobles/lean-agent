#!/usr/bin/env bash
# What a change touches that it did not write.
#
# Every defect an adversarial critic found across nine agent-made pull requests in one run was an
# interaction between two things, never a defect inside one thing. An agent has whole context on
# what it builds and partial context on what it calls, so its own tests cover what it thought of.
# This prints the part it did not think of, before the push, where a finding costs one local test
# run instead of a review round trip.
#
# It answers two questions about a diff:
#
#   1. Which unchanged files use a symbol this diff changed? Those are the callers whose behaviour
#      the change can alter without any test in the diff noticing.
#   2. Which unchanged files import a changed file? Those are the modules the change reaches.
#
# It finds candidates, it does not judge them. The output is the list the author has to account
# for: for each entry, either a test that crosses that boundary, or a sentence saying why not.
#
# Usage:  interaction-surface.sh [base-ref]            (default: origin/master)
#         interaction-surface.sh --wip                 (uncommitted work against HEAD)
#         interaction-surface.sh --symbols [base-ref]  (also list exported-symbol callers)
#
# The symbol list is off by default. Across three changes it produced no defect, while the import
# list produced the one real find, so paying to read it by default was not earning its place.
#
# Exit codes: 0 always. This reports, it does not gate. A gate that guesses gets switched off.

set -uo pipefail

WITH_SYMBOLS=0
if [ "${1:-}" = "--symbols" ]; then WITH_SYMBOLS=1; shift; fi
BASE=${1:-origin/master}
if [ "$BASE" = "--wip" ]; then BASE=HEAD; fi

if ! git rev-parse --git-dir >/dev/null 2>&1; then
    echo "interaction-surface: not a git repository" >&2
    exit 0
fi

if ! git rev-parse --verify --quiet "$BASE" >/dev/null; then
    echo "interaction-surface: no such ref '$BASE'. Pass a base ref, or fetch first." >&2
    exit 0
fi

CHANGED=$(git diff --name-only "$BASE" -- 2>/dev/null | grep -vE '(^|/)(node_modules|dist|bin|obj|\.next|vendor|packages\.lock\.json)(/|$)' || true)
if [ -z "$CHANGED" ]; then
    echo "interaction-surface: no changed files against $BASE"
    exit 0
fi

echo "Interaction surface against $BASE"
echo

# Exported symbols this diff defines, taken from added lines only. Exported, not merely top level:
# the first version matched module-local names too, and on a real branch half the output was a
# local called `order` or `base` colliding with an unrelated file's local of the same name. A
# caller cannot depend on what it cannot reach, so a name nothing exports is not a boundary.
# The patterns cover the declaration forms of the languages this has been used on; an unmatched
# language yields fewer candidates rather than a wrong answer, which is the direction to prefer.
SYMBOLS=$(git diff "$BASE" -- $CHANGED 2>/dev/null \
    | grep '^+' | grep -v '^+++' \
    | sed 's/^+//' \
    | grep -vE '^[[:space:]]*(//|\*|/\*|#|--|<!--)' \
    | grep -oE '^(export|public|internal|protected)[[:space:]]+([A-Za-z]+[[:space:]]+)*(function|const|let|var|class|interface|type|enum|def|record|struct)[[:space:]]+[A-Za-z_][A-Za-z0-9_]*' \
    | awk '{print $NF}' \
    | sort -u)

# A name shorter than four characters matches half the tree and tells nobody anything.
SYMBOLS=$(echo "$SYMBOLS" | awk 'length($0) >= 4')

CHANGED_RE=$(echo "$CHANGED" | sed 's/[].[^$\\*/]/\\&/g' | paste -sd'|' -)

found_any=0
if [ "$WITH_SYMBOLS" = "1" ]; then
echo "== Unchanged files using a symbol this diff defines =="
echo
while IFS= read -r sym; do
    [ -z "$sym" ] && continue
    users=$(git grep -lw -- "$sym" 2>/dev/null \
        | grep -vE '(^|/)(node_modules|dist|bin|obj|\.next|vendor)(/|$)' \
        | grep -vE '\.(md|markdown|html|htm|txt|json|ya?ml|lock|csv|svg|png|jpe?g)$' \
        | grep -vE "^($CHANGED_RE)$" \
        | head -8)
    if [ -n "$users" ]; then
        found_any=1
        count=$(echo "$users" | wc -l | tr -d ' ')
        echo "  $sym  ($count unchanged file(s))"
        echo "$users" | sed 's/^/      /'
    fi
done <<< "$SYMBOLS"
[ "$found_any" = "0" ] && echo "  none"
echo
fi

echo "== Unchanged files importing a changed file =="
echo
found_any=0
while IFS= read -r f; do
    [ -z "$f" ] && continue
    stem=$(basename "$f" | sed 's/\.[^.]*$//')
    [ ${#stem} -lt 4 ] && continue
    importers=$(git grep -lE "(import|require|from|using|include)[^\n]*[\"'/.]${stem}([\"'./]|$)" 2>/dev/null \
        | grep -vE '(^|/)(node_modules|dist|bin|obj|\.next|vendor)(/|$)' \
        | grep -vE "^($CHANGED_RE)$" \
        | head -6)
    if [ -n "$importers" ]; then
        found_any=1
        echo "  $f"
        echo "$importers" | sed 's/^/      /'
    fi
done <<< "$CHANGED"
[ "$found_any" = "0" ] && echo "  none"
echo

cat <<'NOTE'
== What to do with this ==

For each entry above, one of two things must be true before the change is pushed:

  - a test crosses that boundary, exercising the caller against the changed behaviour; or
  - a sentence in the pull request says why that caller is unaffected.

An entry with neither is where the critic finds its next defect, and finding it there costs a
review round trip rather than one local test run.

Two callers deserve a second look whatever the list says: a path that reads configuration the
change also reads, and a path that runs when the change fails rather than when it succeeds. Both
were real defects that this list would have surfaced and a passing test suite did not.
NOTE
