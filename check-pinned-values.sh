#!/usr/bin/env bash
# Asserts that a value pinned in several files is the same in all of them.
#
#   bash check-pinned-values.sh <file>:<sed-capture> [<file>:<sed-capture> ...]
#
# The capture is a sed expression producing exactly the value on stdout. Example, a .NET release
# where the core version, a template pack and the version that template scaffolds against all move
# together:
#
#   bash check-pinned-values.sh \
#     'src/Core/Core.csproj:s|.*<Version>\([^<]*\)</Version>.*|\1|p' \
#     'src/Templates/Templates.csproj:s|.*<Version>\([^<]*\)</Version>.*|\1|p' \
#     'src/Templates/t/.template.config/template.json:s|.*"defaultValue"[[:space:]]*:[[:space:]]*"\([^"]*\)".*|\1|p'
#
# Why this exists. A release moves a version in one file and a human or an agent forgets the others.
# Something downstream usually notices, but late: in my case an integration test caught it eighteen
# minutes into CI, twice in one afternoon, on a release that was otherwise ready. The test was doing
# its job. Nothing was doing this job.
#
# Two deliberate choices worth copying.
#
# A string compare, not a semver parse. These files are the same release or one of them is a
# mistake. A parse would let 1.2.0 and 1.2 agree when the files have to match literally.
#
# An empty capture fails rather than reading as agreement. If a file's shape changes, the sed
# produces nothing, and two nothings are equal. That is how a check quietly stops checking while
# still reporting success, which is the failure this whole script exists to prevent.

set -uo pipefail

[ $# -ge 2 ] || {
  echo "check-pinned-values: give at least two <file>:<sed-capture> pairs, or there is nothing to compare."
  exit 1
}

names=()
values=()
expected=""

for pair in "$@"; do
  file=${pair%%:*}
  expr=${pair#*:}

  [ -f "$file" ] || { echo "check-pinned-values: $file is missing"; exit 1; }

  value=$(sed -n "$expr" "$file" | head -1)
  if [ -z "$value" ]; then
    echo "check-pinned-values: read nothing from $file, so its shape has changed."
    echo "                     Update the expression rather than deleting the check."
    exit 1
  fi

  names+=("$file")
  values+=("$value")
  [ -n "$expected" ] || expected="$value"
done

ok=1
for v in "${values[@]}"; do
  [ "$v" = "$expected" ] || ok=0
done

if [ "$ok" -eq 1 ]; then
  echo "All ${#values[@]} pinned values agree on $expected."
  exit 0
fi

echo "check-pinned-values: the pinned values disagree."
i=0
while [ $i -lt ${#names[@]} ]; do
  if [ "${values[$i]}" = "$expected" ]; then
    printf '  %-60s %s\n' "${names[$i]}" "${values[$i]}"
  else
    printf '  %-60s %s   <- expected %s\n' "${names[$i]}" "${values[$i]}" "$expected"
  fi
  i=$((i + 1))
done
echo
echo "The first file listed sets the expected value. If the others are right and it is wrong,"
echo "fix it there rather than here."
exit 1
