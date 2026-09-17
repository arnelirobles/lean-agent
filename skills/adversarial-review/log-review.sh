#!/usr/bin/env bash
# Append one review outcome to review-log.tsv, for comparing what the critic caught against CI and bots.
# Usage: log-review.sh <repo> <pr> <confirmed> <refuted> <blockers> "<categories>" "<caught elsewhere first>"
set -euo pipefail
if [ "$#" -ne 7 ]; then
  echo "usage: log-review.sh <repo> <pr> <confirmed> <refuted> <blockers> \"<categories>\" \"<caught elsewhere first>\"" >&2
  exit 2
fi
for n in "$3" "$4" "$5"; do
  case "$n" in ''|*[!0-9]*) echo "confirmed, refuted and blockers must be whole numbers" >&2; exit 2;; esac
done
log="${REVIEW_LOG:-review-log.tsv}"
if [ ! -f "$log" ]; then
  printf 'date\trepo\tpr\tconfirmed\trefuted\tblockers\tcategories\tcaught_elsewhere_first\n' > "$log"
fi
clean() { printf '%s' "$1" | tr '\t\n' '  '; }
printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$(date -u +%Y-%m-%d)" "$(clean "$1")" "$(clean "$2")" "$3" "$4" "$5" "$(clean "$6")" "$(clean "$7")" >> "$log"
echo "logged to $log"
