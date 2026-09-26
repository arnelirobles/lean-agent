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
# It reports and never writes anywhere but stdout. Needs gh and jq.
set -euo pipefail

row() { # repo pr opened merged first_review_at commits_after failed_checks
  local days
  days=$(( ( $(date -d "$4" +%s) - $(date -d "$3" +%s) ) / 86400 ))
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
  gh pr list -R "$repo" --state merged --search "merged:>=$since" --limit 200 \
    --json number,createdAt,mergedAt,headRefOid > /tmp/retro.$$.json
  for n in $(jq -r '.[].number' /tmp/retro.$$.json); do
    opened=$(jq -r ".[] | select(.number==$n) | .createdAt" /tmp/retro.$$.json)
    merged=$(jq -r ".[] | select(.number==$n) | .mergedAt" /tmp/retro.$$.json)
    # Reviews here mostly happen outside GitHub's review feature, so a fix round is a commit pushed
    # after the pull request opened, not counting merges of the default branch.
    first_review=$opened
    after=$(gh api "repos/$repo/pulls/$n/commits" --paginate -q "[.[] | select(.commit.committer.date > \"$opened\") | select(.commit.message | startswith(\"Merge\") | not)] | length" 2>/dev/null | awk '{s+=$1} END{print s+0}')
    head=$(jq -r ".[] | select(.number==$n) | .headRefOid" /tmp/retro.$$.json)
    failed=$(gh api "repos/$repo/commits/$head/check-runs" -q '[.check_runs[] | select(.conclusion=="failure")] | length' 2>/dev/null || echo 0)
    escaped=$(gh search issues "repo:$repo \"#$n\" fix in:body created:>$merged" --json number -q 'length' 2>/dev/null || echo 0)
    logged=unknown
    if [ -n "${REVIEW_LOG:-}" ] && [ -f "$REVIEW_LOG" ]; then
      if awk -F'\t' -v r="$repo" -v p="$n" '$0 ~ r && $0 ~ ("\t" p "\t") {f=1} END{exit !f}' "$REVIEW_LOG"; then logged=yes; else logged=no; fi
    fi
    printf '%s\t%s\n' "$(row "$repo" "$n" "$opened" "$merged" "$first_review" "$after" "$failed")" "$escaped	$logged"
  done
done
rm -f /tmp/retro.$$.json
