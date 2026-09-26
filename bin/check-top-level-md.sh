#!/usr/bin/env bash
# Fails when a change adds a Markdown file at the repository root that is not on the allowlist, or
# one named like a pull request body or scratch notes (pr*, body*, notes*).
#
#   check-top-level-md.sh [base]     (base defaults to $LEAN_BASE, then origin/HEAD, origin/main, origin/master)
#   check-top-level-md.sh --self-test
#
# Why this exists. Two pull request bodies, body517.md and body518.md, were committed to a default
# branch by an agent that wrote them in the working tree and then ran `git add -A`. Nothing failed:
# they were valid Markdown in a place Markdown lives. A pull request body belongs in a scratch
# directory outside the repository.
#
# Allowed without asking: README, LICENSE, CHANGELOG, CONTRIBUTING, SECURITY, CODE_OF_CONDUCT,
# CLAUDE and AGENTS (.md). A repository adds its own names, one per line, in
# .lean/top-level-md.allow; shell globs work there. A name matching pr*, body* or notes* fails
# unless the allow file lists it exactly, since a glob that happens to cover it is not a decision.
#
# New means added against the merge base with the base branch, or untracked and not ignored.
# Existing files are never reported. Prints how many new top-level Markdown files it examined;
# zero is a normal answer here.

set -uo pipefail

DEFAULT_ALLOW="README.md LICENSE.md CHANGELOG.md CONTRIBUTING.md SECURITY.md CODE_OF_CONDUCT.md CLAUDE.md AGENTS.md"

default_base() {
  local b
  if [ -n "${LEAN_BASE:-}" ]; then echo "$LEAN_BASE"; return; fi
  b=$(git symbolic-ref -q --short refs/remotes/origin/HEAD 2>/dev/null) && { echo "$b"; return; }
  for b in origin/main origin/master main master; do
    git rev-parse -q --verify "$b^{commit}" >/dev/null 2>&1 && { echo "$b"; return; }
  done
}

check() { # $1 optional base; runs in the repository root
  local base mb new f n=0 bad=0 allowed a exact
  base=${1:-$(default_base)}
  mb=$( [ -n "$base" ] && git merge-base "$base" HEAD 2>/dev/null ) || mb=""
  if [ -z "$mb" ]; then
    echo "check-top-level-md: no merge base with '${base:-a default branch}', so nothing was checked. Pass the base branch."
    return 2
  fi
  new=$( { git diff --name-only --diff-filter=A "$mb" -- ; git ls-files --others --exclude-standard; } \
    | grep -E '^[^/]+\.[Mm][Dd]$' | sort -u)
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    n=$((n + 1))
    allowed=0; exact=0
    for a in $DEFAULT_ALLOW; do [ "$f" = "$a" ] && allowed=1 && exact=1; done
    if [ -f .lean/top-level-md.allow ]; then
      while IFS= read -r a; do
        a=${a%%#*}; a=$(printf '%s' "$a" | tr -d '[:space:]')
        [ -n "$a" ] || continue
        [ "$f" = "$a" ] && exact=1
        # shellcheck disable=SC2254
        case "$f" in $a) allowed=1 ;; esac
      done < .lean/top-level-md.allow
    fi
    shopt -s nocasematch
    case "$f" in
      pr*|body*|notes*)
        if [ "$exact" = 0 ]; then
          echo "  $f: named like a pull request body or scratch notes; write it outside the repository"
          bad=$((bad + 1)); shopt -u nocasematch; continue
        fi ;;
    esac
    shopt -u nocasematch
    if [ "$allowed" = 0 ]; then
      echo "  $f: not on the allowlist; move it under docs/ or add it to .lean/top-level-md.allow if it belongs at the root"
      bad=$((bad + 1))
    fi
  done <<< "$new"
  if [ "$bad" != 0 ]; then
    echo "check-top-level-md: $bad of $n new top-level Markdown file(s) refused (above)"
    return 1
  fi
  echo "check-top-level-md: examined $n new top-level Markdown file(s) against $base, all allowed"
}

self_test() {
  local fails=0 out
  t=$(mktemp -d)
  trap 'rm -rf "$t"' EXIT
  cd "$t" && git init -q -b main . && git config user.email t@example.com && git config user.name t || return 1
  echo x > README.md && git add -A && git commit -qm base && git branch base
  expect() { # $1 label, $2 want exit, $3 pattern
    out=$(check base); local rc=$?
    { [ "$rc" = "$2" ] && grep -qE "$3" <<<"$out"; } || { echo "self-test failed: $1: exit $rc want $2: $out"; fails=$((fails + 1)); }
  }
  expect "nothing new" 0 'examined 0 new'
  echo x > CHANGELOG.md; git add CHANGELOG.md
  expect "default allowlist, staged" 0 'examined 1 new'
  echo x > pr-body.md
  expect "untracked pr body" 1 'pr-body.md: named like a pull request body'
  rm pr-body.md; echo x > NOTES.md; git add NOTES.md; git commit -qm notes
  expect "committed notes, any case" 1 'NOTES.md: named like'
  git rm -q NOTES.md; git commit -qm rm; echo x > DESIGN.md
  expect "unlisted name" 1 'DESIGN.md: not on the allowlist'
  mkdir -p .lean; printf 'DESIGN.md\n# comment\nADR-*.md\np*.md\n' > .lean/top-level-md.allow
  expect "allow file exact" 0 'examined 2 new'
  echo x > ADR-001.md
  expect "allow file glob" 0 'examined 3 new'
  echo x > plan.md
  expect "glob allows a non-pr name" 0 'examined 4 new'
  echo x > pr.md
  expect "glob does not allow a pr name" 1 'pr.md: named like'
  rm pr.md; mkdir -p docs; echo x > docs/pr-notes.md
  expect "nested file is not top-level" 0 'examined 4 new'
  expect_base() { out=$(check no-such-branch); [ $? = 2 ] || { echo "self-test failed: bad base did not exit 2"; fails=$((fails + 1)); }; }
  expect_base
  [ "$fails" = 0 ] && echo "self-test passed" && return 0
  return 1
}

if [ "${1:-}" = "--self-test" ]; then self_test; exit $?; fi
top=$(git rev-parse --show-toplevel 2>/dev/null) || { echo "check-top-level-md: not inside a git repository" >&2; exit 2; }
cd "$top" && check "${1:-}"
