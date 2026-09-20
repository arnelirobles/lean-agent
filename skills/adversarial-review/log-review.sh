#!/usr/bin/env bash
# Append one review outcome to review-log.tsv.
#
# The point of the extra columns is that "fewer review findings" is not a goal you can chase
# directly. A change with no findings is either a clean change or a weak review, and the count alone
# cannot tell you which. On one real pull request the bot returned nothing at all, and that change
# held two silent overwrites that lost somebody's edit with no error anywhere. By finding count it
# was the best change of the day. It was the most dangerous.
#
# So record where each finding was caught, and record what escaped. Then the claims become testable:
#
#   findings move earlier   ->  pre_push rises as a share, without total falling
#   the code is getting better  ->  escaped falls
#   the review is still working ->  review stays non-zero while escaped stays at zero
#
# A drop in review with a drop in escaped is the method working. A drop in review with escaped
# rising is the review going soft, which looks identical if you only count findings.
#
# Usage:
#   log-review.sh <repo> <pr> <confirmed> <refuted> <blockers> "<categories>" "<caught elsewhere first>" \
#                 [pre_push] [ci] [review] [escaped]
#
# The last four are counts and default to 0, so existing seven-argument calls still work.
# `escaped` is normally filled in later: when a defect is traced back to a merged change, amend that
# row rather than adding a new one, or the number never moves.
set -euo pipefail

if [ "$#" -lt 7 ] || [ "$#" -gt 11 ]; then
  echo "usage: log-review.sh <repo> <pr> <confirmed> <refuted> <blockers> \"<categories>\" \"<caught elsewhere first>\" [pre_push] [ci] [review] [escaped]" >&2
  exit 2
fi

PRE_PUSH=${8:-0}
IN_CI=${9:-0}
IN_REVIEW=${10:-0}
ESCAPED=${11:-0}

for n in "$3" "$4" "$5" "$PRE_PUSH" "$IN_CI" "$IN_REVIEW" "$ESCAPED"; do
  case "$n" in ''|*[!0-9]*) echo "counts must be whole numbers" >&2; exit 2;; esac
done

log="${REVIEW_LOG:-review-log.tsv}"
header=$'date\trepo\tpr\tconfirmed\trefuted\tblockers\tcategories\tcaught_elsewhere_first\tpre_push\tci\treview\tescaped'

if [ ! -f "$log" ]; then
  printf '%s\n' "$header" > "$log"
elif [ "$(head -1 "$log")" != "$header" ]; then
  # An older log has eight columns. Widen it in place rather than starting a second file, so the
  # history before the columns existed stays comparable with what comes after.
  tmp=$(mktemp)
  { printf '%s\n' "$header"
    tail -n +2 "$log" | awk -F'\t' 'BEGIN{OFS="\t"} {print $0, 0, 0, 0, 0}'
  } > "$tmp"
  mv "$tmp" "$log"
fi

clean() { printf '%s' "$1" | tr '\t\n' '  '; }
printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
  "$(date -u +%Y-%m-%d)" "$(clean "$1")" "$(clean "$2")" "$3" "$4" "$5" \
  "$(clean "$6")" "$(clean "$7")" "$PRE_PUSH" "$IN_CI" "$IN_REVIEW" "$ESCAPED" >> "$log"
echo "logged to $log"
