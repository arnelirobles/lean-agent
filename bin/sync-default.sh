#!/usr/bin/env bash
# Merges the default branch into the current branch, then restores dependencies in locked mode
# before anything builds.
#
#   sync-default.sh [--no-restore]
#   sync-default.sh --self-test
#
# Steps, each stopping the script with a named reason when it fails:
#   1. refuse a dirty working tree (tracked changes or untracked files)
#   2. find the default branch: origin/HEAD, else what the remote reports, else origin/main or origin/master
#   3. fetch and merge it; on conflict, list the conflicted files and exit 1 with the merge left in
#      progress for you to resolve or abort
#   4. for each lock file at the repository root, run its locked restore:
#        package-lock.json  npm ci                              pnpm-lock.yaml  pnpm install --frozen-lockfile
#        yarn.lock          yarn install --immutable (berry) or --frozen-lockfile (classic)
#        packages.lock.json (anywhere)  dotnet restore --locked-mode
#        go.sum             go mod verify                        Cargo.lock      cargo fetch --locked
#
# Why the restore. A build's own implicit restore rewrites a stale lock file instead of failing, so
# a merge that changed a dependency on one side and the lock file on the other builds green here and
# fails in CI, or worse, does not. A locked restore right after the merge fails loudly instead.
# Restores run through heavy.sh (lanes node, dotnet, go, cargo) so parallel agents queue them.
#
# It never discards work: no reset, no checkout of files, no stash, no merge --abort. The dirty-tree
# refusal is what makes that safe, because git refuses a merge over local changes anyway, and a
# refusal that reads "dirty tree" is clearer than an empty conflict list.
#
# LEAN_SYNC_DRY_RESTORE=1 prints the restore commands instead of running them (the self-test uses it).

set -uo pipefail

here=$(dirname "$(readlink -f "$0")")
fail() { echo "sync-default: $1"; exit 1; }

default_branch() {
  local b
  b=$(git symbolic-ref -q --short refs/remotes/origin/HEAD 2>/dev/null) && { echo "${b#origin/}"; return; }
  b=$(git remote show origin 2>/dev/null | sed -n 's/^ *HEAD branch: //p')
  [ -n "$b" ] && [ "$b" != "(unknown)" ] && { echo "$b"; return; }
  for b in main master; do
    git rev-parse -q --verify "origin/$b^{commit}" >/dev/null 2>&1 && { echo "$b"; return; }
  done
}

restore_plan() { # prints "lane<TAB>command" lines for the lock files present, in the repository root
  [ -f package-lock.json ] && printf 'node\tnpm ci\n'
  [ -f pnpm-lock.yaml ] && printf 'node\tpnpm install --frozen-lockfile\n'
  if [ -f yarn.lock ]; then
    if [ -f .yarnrc.yml ]; then printf 'node\tyarn install --immutable\n'; else printf 'node\tyarn install --frozen-lockfile\n'; fi
  fi
  [ -n "$(git ls-files -- '*packages.lock.json' ':(glob)**/packages.lock.json' 2>/dev/null | head -1)" ] && printf 'dotnet\tdotnet restore --locked-mode\n'
  [ -f go.sum ] && printf 'go\tgo mod verify\n'
  [ -f Cargo.lock ] && printf 'cargo\tcargo fetch --locked\n'
  return 0
}

restore() {
  local plan lane cmd n=0
  plan=$(restore_plan)
  if [ -z "$plan" ]; then
    echo "sync-default: no lock files at the root, nothing to restore"
    return 0
  fi
  while IFS=$'\t' read -r lane cmd; do
    n=$((n + 1))
    if [ "${LEAN_SYNC_DRY_RESTORE:-}" = "1" ]; then
      echo "would run [$lane]: $cmd"
      continue
    fi
    echo "== locked restore [$lane]: $cmd =="
    # shellcheck disable=SC2086
    bash "$here/heavy.sh" "$lane" $cmd || fail "'$cmd' failed. If it says the lock file does not match, regenerate it with the unlocked command, check the diff, and commit it."
  done <<< "$plan"
  echo "sync-default: $n locked restore(s) done"
}

sync() {
  local no_restore=$1 top def conflicts
  top=$(git rev-parse --show-toplevel 2>/dev/null) || fail "not inside a git repository"
  cd "$top" || fail "cannot enter $top"
  [ -z "$(git status --porcelain)" ] || fail "working tree has uncommitted changes or untracked files; commit them first (this script never stashes or discards)"
  git fetch -q origin || fail "git fetch origin failed"
  def=$(default_branch)
  [ -n "$def" ] || fail "cannot tell the default branch; set it with: git remote set-head origin --auto"
  [ "$(git rev-parse --abbrev-ref HEAD)" != "$def" ] || echo "sync-default: note, you are on $def itself"
  echo "== merge origin/$def =="
  if ! git merge --no-edit "origin/$def"; then
    conflicts=$(git diff --name-only --diff-filter=U)
    [ -n "$conflicts" ] || fail "merge failed without conflicts; read git's message above"
    echo "sync-default: merge conflicts in:"
    printf '  %s\n' $conflicts
    echo "Resolve them and commit, or run 'git merge --abort' yourself. Nothing was restored."
    exit 1
  fi
  [ "$no_restore" = 1 ] && { echo "sync-default: merged origin/$def, restore skipped (--no-restore)"; return 0; }
  restore
  echo "sync-default: merged origin/$def cleanly"
}

self_test() {
  local fails=0 out rc
  t=$(mktemp -d)
  trap 'rm -rf "$t"' EXIT
  export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.com GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.com
  git init -q --bare -b main "$t/origin.git"
  git clone -q "$t/origin.git" "$t/seed" 2>/dev/null
  ( cd "$t/seed" && git checkout -q -b main && printf 'a\n' > f.txt && printf '{}\n' > package-lock.json && git add -A && git commit -qm base && git push -q origin main )
  git clone -q "$t/origin.git" "$t/work"
  ( cd "$t/seed" && printf 'b\n' > g.txt && git add -A && git commit -qm upstream && git push -q origin main )
  cd "$t/work" && git checkout -q -b feature
  export LEAN_SYNC_DRY_RESTORE=1
  expect() { # $1 label, $2 want exit, $3 pattern
    out=$( (cd "$t/work/${4:-.}" && sync 0) 2>&1); rc=$?
    { [ "$rc" = "$2" ] && grep -qE "$3" <<<"$out"; } || { echo "self-test failed: $1: exit $rc want $2: $out"; fails=$((fails + 1)); }
  }
  echo scratch > untracked.txt
  expect "untracked file refused" 1 'untracked files; commit them first'
  [ -f untracked.txt ] || { echo "self-test failed: the untracked file was removed"; fails=$((fails + 1)); }
  rm untracked.txt; printf 'local\n' >> f.txt
  expect "dirty tree refused" 1 'uncommitted changes'
  grep -q local f.txt || { echo "self-test failed: the local change was discarded"; fails=$((fails + 1)); }
  git commit -qam local
  mkdir -p sub
  expect "clean merge from a subdirectory, restore planned" 0 'would run \[node\]: npm ci' sub
  [ -f g.txt ] || { echo "self-test failed: upstream commit not merged"; fails=$((fails + 1)); }
  ( cd "$t/seed" && printf 'theirs\n' > f.txt && git commit -qam theirs && git push -q origin main )
  printf 'ours\n' > f.txt && git commit -qam ours
  expect "conflict listed" 1 'merge conflicts in:.*'
  grep -q '  f.txt' <<<"$out" || { echo "self-test failed: conflicted file not named: $out"; fails=$((fails + 1)); }
  git merge --abort
  git remote set-head origin -d
  out=$(default_branch); [ "$out" = main ] || { echo "self-test failed: default branch fallback gave '$out'"; fails=$((fails + 1)); }
  git rm -q package-lock.json && printf 'x\n' > go.sum && git add go.sum && git commit -qm go
  out=$(restore_plan); [ "$out" = "$(printf 'go\tgo mod verify')" ] || { echo "self-test failed: plan for go.sum was '$out'"; fails=$((fails + 1)); }
  [ "$fails" = 0 ] && echo "self-test passed" && return 0
  return 1
}

case "${1:-}" in
  --self-test) self_test; exit $? ;;
  --no-restore) sync 1 ;;
  "") sync 0 ;;
  *) echo "usage: sync-default.sh [--no-restore]" >&2; exit 2 ;;
esac
