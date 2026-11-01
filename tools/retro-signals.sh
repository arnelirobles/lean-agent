#!/usr/bin/env bash
# Collect the numbers a retro needs for one batch, without reasoning, so the retro starts from facts.
#
#   retro-signals.sh --since 2026-09-24 REPO [REPO ...]   one TSV row per merged pull request
#   retro-signals.sh --self-test                                       checks the parsing on a fixture
#
# After the rows it prints a script repeats section (tools/script-repeats): the families of commands agents
# wrote by hand in the last SCRIPT_REPEATS_DAYS days (default 7), with counts only. SCRIPT_REPEATS_ROOT
# points it at another transcript directory.
#
# Per merged pull request: commits pushed after it opened, not counting merges (fix rounds), failed check runs,
# days from open to merge, and later pull requests or issues in the same repository whose text points
# back at it as the cause of a fix (an escaped defect, a proxy that undercounts rather than guesses).
# If REVIEW_LOG points at the adversarial review log, rows for pull requests with no log entry are
# counted, because a review nobody logged is a loop that is not running.
#
# REPO is owner/name (GitHub), HOST/group/project or a clone URL (GitLab), or an Azure DevOps clone URL
# (lib/forge.py explains the names). It reports and never writes anywhere but stdout. Needs the CLI of the
# host (gh, glab or az) and jq for the day count. Every read goes through lib/forge.py; a host whose CLI is not
# installed stops with one line naming the CLI to install, and exit 2. On Azure DevOps the failed check and
# escaped counts print unknown, because az cannot find them.
set -euo pipefail
lib="$(cd "$(dirname "$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$0")")/../lib" && pwd)"

row() { # repo pr opened merged first_review_at commits_after failed_checks
  local days
  # jq rather than `date -d`, which BSD date on macOS does not have.
  days=$(jq -n --arg a "$3" --arg b "$4" '(($b | fromdateiso8601) - ($a | fromdateiso8601)) / 86400 | floor')
  printf '%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$6" "$7" "$days"
}

repeats_section() {
  local bound=""
  # macOS has no timeout unless coreutils is installed, as gtimeout. Without either it runs unbounded.
  if command -v timeout >/dev/null 2>&1; then bound="timeout 120"
  elif command -v gtimeout >/dev/null 2>&1; then bound="gtimeout 120"; fi
  printf '\n# script repeats\n'
  $bound python3 -I "$lib/../tools/script-repeats" --days "${SCRIPT_REPEATS_DAYS:-7}" ${SCRIPT_REPEATS_ROOT:+--root "$SCRIPT_REPEATS_ROOT"} \
    || echo "script-repeats failed"
}

if [ "${1:-}" = "--self-test" ]; then
  out=$(row BaryoDev/demo 42 2026-09-20T00:00:00Z 2026-09-23T12:00:00Z 2026-09-21T00:00:00Z 2 1)
  want=$(printf 'BaryoDev/demo\t42\t2\t1\t3')
  [ "$out" = "$want" ] || { echo "self-test failed: got '$out', want '$want'"; exit 1; }
  # A gh that lists one merged pull request and fails every other call must give unknown, never 0.
  bindir=$(mktemp -d)
  printf '%s\n' '#!/bin/sh' 'case "$*" in "pr list"*) echo "[{\"number\":5,\"headRefName\":\"b\",\"headRefOid\":\"abc\",\"createdAt\":\"2026-09-20T00:00:00Z\",\"mergedAt\":\"2026-09-22T00:00:00Z\"}]" ;; *) exit 1 ;; esac' > "$bindir/gh"
  chmod +x "$bindir/gh"
  out=$(PATH="$bindir:$PATH" "$0" --since 2026-09-01 o/r 2>/dev/null | sed -n 2p)
  rm -rf "$bindir"
  want=$(printf 'o/r\t5\tunknown\tunknown\t2\tunknown\tunknown')
  [ "$out" = "$want" ] || { echo "self-test failed: failed gh calls gave '$out', want '$want'"; exit 1; }
  # A fake GitLab repository: a glab that answers the merge request reads. Rows, not exit 2.
  bindir=$(mktemp -d)
  cat > "$bindir/glab" <<'G'
#!/bin/sh
for last; do :; done
case "$last" in
  */merge_requests?state=merged*) echo '[{"iid":5,"source_branch":"b","sha":"abcd1234","created_at":"2026-09-20T00:00:00Z","merged_at":"2026-09-22T00:00:00Z","updated_at":"2026-09-22T00:00:00Z"}]' ;;
  */merge_requests/5/commits*) echo '[{"title":"fix","committed_date":"2026-09-21T00:00:00Z"},{"title":"Merge branch main","committed_date":"2026-09-21T01:00:00Z"},{"title":"early","committed_date":"2026-09-19T00:00:00Z"}]' ;;
  */repository/commits/abcd1234/statuses*) echo '[{"status":"failed"},{"status":"success"}]' ;;
  */issues?*|*/merge_requests?search*) echo '[]' ;;
  *) echo "404" >&2; exit 1 ;;
esac
G
  chmod +x "$bindir/glab"
  out=$(PATH="$bindir:$PATH" "$0" --since 2026-09-01 gitlab.example.com/g/r 2>/dev/null; echo "rc=$?")
  rm -rf "$bindir"
  want=$(printf 'gitlab.example.com/g/r\t5\t1\t1\t2\t0\tunknown')
  case "$out" in *"$want"*"rc=0"*) ;; *) echo "self-test failed: a fake GitLab repository gave '$out', want a row '$want'"; exit 1 ;; esac
  # No glab on PATH: one line naming it, exit 2.
  bindir=$(mktemp -d)
  out=$(PATH="$bindir:$(dirname "$(command -v python3)"):/usr/bin:/bin" "$0" --since 2026-09-01 gitlab.example.com/g/r 2>&1 >/dev/null) && rc=0 || rc=$?
  rm -rf "$bindir"
  if ! command -v glab >/dev/null 2>&1; then
    case "$out" in *"glab is not installed"*) [ "$rc" = 2 ] || { echo "self-test failed: missing glab gave exit $rc"; exit 1; } ;; *) echo "self-test failed: missing glab said '$out'"; exit 1 ;; esac
  fi
  fixture=$(mktemp -d)
  mkdir -p "$fixture/proj"
  printf '%s\n' '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"pgrep -f vitest"}}]}}' > "$fixture/proj/s.jsonl"
  out=$(SCRIPT_REPEATS_ROOT="$fixture" repeats_section)
  rm -rf "$fixture"
  case "$out" in *"# script repeats"*) ;; *) echo "self-test failed: no script repeats section"; exit 1 ;; esac
  case "$out" in *"pgrep"*"hooks/wait-loop.py"*) ;; *) echo "self-test failed: pgrep family missing: $out"; exit 1 ;; esac
  echo "self-test passed"
  exit 0
fi

[ "${1:-}" = "--since" ] && [ -n "${2:-}" ] || { echo "usage: retro-signals.sh --since YYYY-MM-DD REPO ..."; exit 2; }
since=$2; shift 2
[ "$#" -gt 0 ] || { echo "name at least one REPO"; exit 2; }

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
repeats_section
