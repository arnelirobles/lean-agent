#!/usr/bin/env bash
# Fails when any tracked or untracked text file in the repository holds git conflict markers.
#
#   check-conflict-markers.sh
#   check-conflict-markers.sh --self-test
#
# Why this exists. A changelog with conflict markers in it reached a default branch, because
# resolving the same file on eight branches was scripted and nobody read the result. Nothing else
# looked: the markers were in Markdown, so no compiler, linter or test had an opinion, and every
# gate stayed green. Markdown and YAML are the dangerous cases because nothing else parses them.
#
# What counts. A line starting "<<<<<<< ", ">>>>>>> " or "||||||| " is a marker anywhere. A bare
# "=======" line is reported only in a file that also has one of those, because seven equals signs
# also underline a Markdown heading. Binary files, *.patch and *.diff are skipped. Untracked files
# are included (minus ignored ones), since the file an agent resolved and has not added yet is the
# one most likely to be wrong.
#
# Prints how many files it examined, and fails when that is zero: every repository has a file.

set -uo pipefail

scan() { # runs in the repository root
  local n=0 bad=0 f hits
  while IFS= read -r -d '' f; do
    case "$f" in *.patch|*.diff) continue ;; esac
    [ -f "$f" ] || continue
    grep -Iq . "$f" 2>/dev/null || continue
    n=$((n + 1))
    hits=$(grep -nE '^(<<<<<<< |>>>>>>> |\|\|\|\|\|\|\| |<<<<<<<$|>>>>>>>$)' "$f" 2>/dev/null)
    if [ -n "$hits" ]; then
      hits=$(grep -nE '^(<<<<<<< |>>>>>>> |\|\|\|\|\|\|\| |<<<<<<<$|>>>>>>>$|=======$)' "$f")
      bad=$((bad + 1))
      printf '%s\n' "$hits" | sed "s|^|  $f:|"
    fi
  done < <(git ls-files -z --cached --others --exclude-standard | sort -zu)
  if [ "$n" = 0 ]; then
    echo "check-conflict-markers: examined 0 text files, so nothing was checked"
    return 1
  fi
  if [ "$bad" != 0 ]; then
    echo "check-conflict-markers: conflict markers in $bad of $n text files (above)."
    echo "  A merge or rebase was resolved without reading the result. Fix the file, do not just"
    echo "  delete the marker lines: one side of the conflict is usually missing too."
    return 1
  fi
  echo "check-conflict-markers: examined $n text files, no conflict markers"
}

self_test() {
  local fails=0 lt gt eq
  t=$(mktemp -d)
  trap 'rm -rf "$t"' EXIT
  lt=$(printf '<%.0s' 1 2 3 4 5 6 7); gt=$(printf '>%.0s' 1 2 3 4 5 6 7); eq=$(printf '=%.0s' 1 2 3 4 5 6 7)
  cd "$t" && git init -q . || return 1
  (scan) >/dev/null && { echo "self-test failed: an empty repository passed"; fails=$((fails + 1)); }
  printf 'Title\n%s\n\ntext\n' "$eq" > README.md
  git add README.md
  (scan) >/dev/null || { echo "self-test failed: a Markdown heading underline was reported"; fails=$((fails + 1)); }
  printf '%s HEAD\nours\n%s\ntheirs\n%s branch\n' "$lt" "$eq" "$gt" > CHANGELOG.md
  git add CHANGELOG.md
  grep -q 'CHANGELOG.md:1' <<<"$(scan)" || { echo "self-test failed: tracked markers not found"; fails=$((fails + 1)); }
  git rm -q --cached CHANGELOG.md
  grep -q 'CHANGELOG.md:3' <<<"$(scan)" || { echo "self-test failed: untracked markers not found"; fails=$((fails + 1)); }
  echo CHANGELOG.md > .gitignore
  (scan) >/dev/null || { echo "self-test failed: an ignored file was reported"; fails=$((fails + 1)); }
  printf '%s a\n' "$lt" > fix.patch
  (scan) >/dev/null || { echo "self-test failed: a .patch file was reported"; fails=$((fails + 1)); }
  [ "$fails" = 0 ] && echo "self-test passed" && return 0
  return 1
}

if [ "${1:-}" = "--self-test" ]; then self_test; exit $?; fi
top=$(git rev-parse --show-toplevel 2>/dev/null) || { echo "check-conflict-markers: not inside a git repository" >&2; exit 2; }
cd "$top" && scan
