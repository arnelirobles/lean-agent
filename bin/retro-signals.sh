#!/usr/bin/env bash
# Collect the numbers a retro needs for one batch, without reasoning, so the retro starts from facts.
#
#   retro-signals.sh --since 2026-09-24 owner/repo [owner/repo ...]   one TSV row per merged pull request
#   retro-signals.sh --self-test                                       checks the parsing on a fixture
#
# Per merged pull request: commits pushed after it opened, not counting merges (fix rounds), failed check runs,
# days from open to merge, and later pull requests or issues in the same repository whose text points
# back at it as the cause of a fix (an escaped defect, a proxy that undercounts rather than guesses).
# If REVIEW_LOG points at the adversarial review log, rows for pull requests with no log entry are
# counted, because a review nobody logged is a loop that is not running.
#
# It reports and never writes anywhere but stdout. Needs gh, and jq for the day count. The gh reads go
# through lib/forge.py, so a repository on another host stops with one line naming it.
set -euo pipefail
lib="$(cd "$(dirname "$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$0")")/../lib" && pwd)"

row() { # repo pr opened merged first_review_at commits_after failed_checks
  local days
  # jq rather than `date -d`, which BSD date on macOS does not have.
  days=$(jq -n --arg a "$3" --arg b "$4" '(($b | fromdateiso8601) - ($a | fromdateiso8601)) / 86400 | floor')
  printf '%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$6" "$7" "$days"
}

if [ "${1:-}" = "--self-test" ]; then
  out=$(row BaryoDev/demo 42 2026-09-20T00:00:00Z 2026-09-23T12:00:00Z 2026-09-21T00:00:00Z 2 1)
  want=$(printf 'BaryoDev/demo\t42\t2\t1\t3')
  [ "$out" = "$want" ] || { echo "self-test failed: got '$out', want '$want'"; exit 1; }
  echo "self-test passed"
  exit 0
fi

[ "${1:-}" = "--since" ] && [ -n "${2:-}" ] || { echo "usage: retro-signals.sh --since YYYY-MM-DD owner/repo ..."; exit 2; }
since=$2; shift 2
[ "$#" -gt 0 ] || { echo "name at least one owner/repo"; exit 2; }

printf 'repo\tpr\tfix_rounds\tfailed_checks\tdays_to_merge\tescaped_refs\tlogged_review\n'
for repo in "$@"; do
  prs=$(python3 "$lib/forge.py" merged-prs "$repo" "$since")
  while IFS=$'\t' read -r n opened merged head; do
    [ -n "$n" ] || continue
    first_review=$opened
    after=$(python3 "$lib/forge.py" commits-after "$repo" "$n" "$opened")
    failed=$(python3 "$lib/forge.py" failed-checks "$repo" "$head")
    escaped=$(python3 "$lib/forge.py" escaped-refs "$repo" "$n" "$merged")
    logged=unknown
    if [ -n "${REVIEW_LOG:-}" ] && [ -f "$REVIEW_LOG" ]; then
      if awk -F'\t' -v r="$repo" -v p="$n" '$0 ~ r && $0 ~ ("\t" p "\t") {f=1} END{exit !f}' "$REVIEW_LOG"; then logged=yes; else logged=no; fi
    fi
    printf '%s\t%s\n' "$(row "$repo" "$n" "$opened" "$merged" "$first_review" "$after" "$failed")" "$escaped	$logged"
  done <<EOF_PRS
$prs
EOF_PRS
done
