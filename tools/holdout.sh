#!/usr/bin/env bash
# Asserts that a test added by this change notices the change it claims to test.
#
#   bash holdout.sh --spec change-description.md
#   bash holdout.sh --spec change-description.md --dry-run
#   bash holdout.sh --self-test
#
# The spec is a fenced ```holdout block in the change description, binding each new test to the
# production hunk it depends on. This script holds that hunk out (reverts it), rebuilds, and
# requires the named test to fail. A test that passes with its hunk held out does not test it.
#
#   ```holdout
#   - test: Some.Tests.RedeemTests
#     breaks: src/Features/Redeem.cs "RevokedAt is not null"
#   - untested: src/Features/Endpoints.cs #1
#     why: route registration, covered by the inventory check
#   ```
#
# Why this exists. My method described this check for two weeks before I wrote it. The preflight
# script had no revert step, no change description carried a binding, and the review checklist item
# reading "was the mutation reverted and did the named tests fail" was answered yes, by a reviewer,
# on changes where nothing was reverted. The gate written to catch gates that cannot fail was one.
#
# Four choices worth copying.
#
# 1. A held-out tree that does not compile is INCONCLUSIVE, never a pass. Reverting one hunk of a
#    typed language usually breaks the build, and a tree that does not build runs no tests. Score
#    that as success and the gate is green forever over a check that never ran.
# 2. An unclaimed production hunk fails, and is named by file and line. One binding or untested
#    entry claims the one hunk its anchor names, not the whole file. "untested: path" with no
#    anchor exempts every hunk of that file. An untested anchor that matches nothing is unresolved.
#    Hunks are cut with no context lines, so edits a few lines apart are separate hunks. A file
#    with no text hunk (binary, mode change) needs "untested: path". Production is every changed
#    file that is not a test, doc or lockfile (DEFAULT_NOT_PRODUCTION below, replaced by
#    HOLDOUT_NOT_PRODUCTION). HOLDOUT_PRODUCTION narrows it; both are space-separated. Code it
#    leaves out is listed in the result, and a narrowing that matches no changed code fails.
#    The base is HOLDOUT_BASE, else origin/HEAD, origin/main, origin/master.
# 3. Anchors are line content, not line numbers, so a binding survives a rebase.
# 4. Distinct exit codes, so a caller cannot collapse them into "non-zero, whatever":
#      0 pass          held out, the test failed, restored, the test passed again
#      1 caught        a test passed with its hunk held out
#      2 unresolved    anchor matched no hunk or several, or zero tests ran
#      3 inconclusive  the held-out tree did not build, so nothing was proven
#
# Adapt the two lines marked ADAPT to your runner. Everything else is git and bash.
#
# Runs in a throwaway worktree. Never mutates the caller's tree: reverting a hunk over uncommitted
# work and restoring with git checkout is how uncommitted work gets lost.
set -uo pipefail

# ADAPT: your build and your single-test-class run. The test command is given one argument, the
# test id from the spec, and must run that and nothing else.
HOLDOUT_BUILD_CMD="${HOLDOUT_BUILD_CMD:-dotnet build --configuration Release --no-restore}"
HOLDOUT_TEST_CMD="${HOLDOUT_TEST_CMD:-dotnet test --no-build --configuration Release --filter FullyQualifiedName~}"

self_test() {
  local self t fails=0 rc base
  self=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")
  t=$(mktemp -d)
  trap 'rm -rf "$t"' RETURN

  # A tiny repository: a base commit, then a change that adds the line the fake test looks for.
  mkdir "$t/repo" "$t/bin"
  git -C "$t/repo" init -q
  git -C "$t/repo" config user.email self-test@example.com
  git -C "$t/repo" config user.name self-test
  mkdir "$t/repo/src"
  printf 'one\nold\nthree\n' > "$t/repo/src/a.txt"
  git -C "$t/repo" add -A
  git -C "$t/repo" commit -q -m base
  base=$(git -C "$t/repo" rev-parse HEAD)
  printf 'one\nfeature on\nthree\n' > "$t/repo/src/a.txt"
  git -C "$t/repo" commit -q -am change

  # The fake test passes only while the feature line is present.
  cat > "$t/bin/depends.sh" <<'X'
#!/bin/sh
if grep -q 'feature on' src/a.txt; then echo "Total: 1"; echo "Failed: 0"; else echo "Total: 1"; echo "Failed: 1"; fi
X
  cat > "$t/bin/always.sh" <<'X'
#!/bin/sh
echo "Total: 1"; echo "Failed: 0"
X
  printf '```holdout\n- test: SomeTests\n  breaks: src/a.txt "feature on"\n```\n' > "$t/spec.md"
  printf '```holdout\n- untested: src/a.txt #1\n  why: data only\n```\n' > "$t/untested.md"
  printf '```holdout\n- test: SomeTests\n  breaks: src/a.txt "no such line"\n```\n' > "$t/bad.md"
  printf 'no block here\n' > "$t/none.md"

  # run <fake test script> <holdout script> <spec> [args]; sets rc to the exit code.
  run() {
    local tc="$1" script="$2" spec="$3"; shift 3
    ( cd "$t/repo" && HOLDOUT_BASE="$base" HOLDOUT_BUILD_CMD=true HOLDOUT_TEST_CMD="sh $t/bin/$tc" \
        bash "$script" --spec "$spec" "$@" >"$t/out" 2>&1 )
    rc=$?
  }
  want() {
    [ "$rc" = "$2" ] || { echo "self-test failed: $1: exit $rc, want $2"; sed 's/^/  /' "$t/out"; fails=$((fails + 1)); }
  }

  run depends.sh "$self" "$t/spec.md"
  want "test that depends on its hunk passes" 0
  run always.sh "$self" "$t/spec.md"
  want "test that ignores its hunk is caught" 1
  run depends.sh "$self" "$t/spec.md" --dry-run
  want "dry run resolves the anchor" 0
  run depends.sh "$self" "$t/untested.md"
  want "only untested hunks pass without a build" 0
  run depends.sh "$self" "$t/bad.md"
  want "anchor that matches nothing is unresolved" 2
  run depends.sh "$self" "$t/none.md"
  want "spec without a block is refused" 2

  # runin <dir> <base or empty> <fake test> <spec> [args]; empty base leaves HOLDOUT_BASE unset so
  # the default branch detection runs. Sets rc.
  runin() {
    local d="$1" b="$2" tc="$3" spec="$4"; shift 4
    ( cd "$d" && env -u HOLDOUT_BASE ${b:+HOLDOUT_BASE="$b"} HOLDOUT_BUILD_CMD=true \
        HOLDOUT_TEST_CMD="sh $t/bin/$tc" bash "$self" --spec "$spec" "$@" >"$t/out" 2>&1 )
    rc=$?
  }
  mkrepo() {
    mkdir -p "$1"; git -C "$1" init -q
    git -C "$1" config user.email self-test@example.com
    git -C "$1" config user.name self-test
  }

  # A file with two separate hunks, and a test that needs both changes.
  mkrepo "$t/m"; mkdir "$t/m/src"
  seq 1 20 | sed 's/^/l/' > "$t/m/src/m.txt"
  git -C "$t/m" add -A; git -C "$t/m" commit -q -m base
  mbase=$(git -C "$t/m" rev-parse HEAD)
  sed -e 's/^l2$/A on/' -e 's/^l18$/B on/' "$t/m/src/m.txt" > "$t/m/x" && mv "$t/m/x" "$t/m/src/m.txt"
  git -C "$t/m" commit -q -am change
  cat > "$t/bin/both.sh" <<'X'
#!/bin/sh
if grep -q 'A on' src/m.txt && grep -q 'B on' src/m.txt; then echo "Total: 1"; echo "Failed: 0"; else echo "Total: 1"; echo "Failed: 1"; fi
X
  printf '```holdout\n- test: T1\n  breaks: src/m.txt "A on"\n```\n' > "$t/one-bind.md"
  printf '```holdout\n- test: T1\n  breaks: src/m.txt "A on"\n- test: T2\n  breaks: src/m.txt "B on"\n```\n' > "$t/two-bind.md"
  printf '```holdout\n- test: T1\n  breaks: src/m.txt "A on"\n- untested: src/m.txt #2\n  why: wiring\n```\n' > "$t/bind-untested.md"
  printf '```holdout\n- test: T1\n  breaks: src/m.txt "A on"\n- test: T2\n  breaks: src/m.txt "B on"\n- untested: src/m.txt #9\n  why: wiring\n```\n' > "$t/untested-ord.md"
  printf '```holdout\n- test: T1\n  breaks: src/m.txt "A on"\n- test: T2\n  breaks: src/m.txt "B on"\n- untested: src/m.txt "no such text"\n  why: wiring\n```\n' > "$t/untested-text.md"
  printf '```holdout\n- untested: src/m.txt\n  why: whole file is data\n```\n' > "$t/untested-file.md"
  runin "$t/m" "$mbase" both.sh "$t/one-bind.md"
  want "two hunks, one binding fails" 2
  grep -q 'src/m.txt line 18' "$t/out" || { echo "self-test failed: unclaimed hunk not named by file and line"; sed 's/^/  /' "$t/out"; fails=$((fails + 1)); }
  runin "$t/m" "$mbase" both.sh "$t/two-bind.md"
  want "two hunks, two bindings passes" 0
  runin "$t/m" "$mbase" both.sh "$t/bind-untested.md"
  want "one binding and one untested hunk passes" 0
  printf '```holdout\n- test: T1\n  breaks: src/m.txt " on"\n```\n' > "$t/ambig.md"
  runin "$t/m" "$mbase" both.sh "$t/ambig.md"
  want "anchor matching two hunks is ambiguous" 2
  grep -q 'ambiguous' "$t/out" || { echo "self-test failed: ambiguity not reported"; fails=$((fails + 1)); }
  runin "$t/m" "$mbase" both.sh "$t/untested-ord.md"
  want "untested ordinal past the last hunk is unresolved" 2
  runin "$t/m" "$mbase" both.sh "$t/untested-text.md"
  want "untested anchor that matches nothing is unresolved" 2
  runin "$t/m" "$mbase" both.sh "$t/untested-file.md"
  want "untested with no anchor exempts the whole file" 0

  # Code in lib/, not src/.
  mkrepo "$t/lib"; mkdir "$t/lib/lib" "$t/lib/tests"
  printf 'def f():\n    return 1\n' > "$t/lib/lib/x.py"
  git -C "$t/lib" add -A; git -C "$t/lib" commit -q -m base
  lbase=$(git -C "$t/lib" rev-parse HEAD)
  printf 'def f():\n    return 2\n' > "$t/lib/lib/x.py"
  printf 'x\n' > "$t/lib/tests/test_x.py"
  git -C "$t/lib" add -A; git -C "$t/lib" commit -q -m change
  printf '```holdout\nnone: nothing to hold out\n```\n' > "$t/none-reason.md"
  printf '```holdout\n- untested: tests/test_x.py #1\n  why: wrong file\n```\n' > "$t/lib-wrong.md"
  printf '```holdout\n- untested: lib/x.py #1\n  why: data\n```\n' > "$t/lib-right.md"
  runin "$t/lib" "$lbase" depends.sh "$t/none-reason.md"
  want "lib/ change with a none declaration is caught" 2
  runin "$t/lib" "$lbase" depends.sh "$t/lib-wrong.md"
  want "lib/ change with the hunk unclaimed is caught" 2
  runin "$t/lib" "$lbase" depends.sh "$t/lib-right.md"
  want "lib/ change claimed as untested passes" 0
  ( cd "$t/lib" && HOLDOUT_PRODUCTION='src/**' HOLDOUT_BASE="$lbase" bash "$self" --spec "$t/none-reason.md" >"$t/out" 2>&1 ); rc=$?
  want "narrow HOLDOUT_PRODUCTION cannot hide a changed code file" 2

  # Default branch detection.
  git -C "$t/lib" update-ref refs/remotes/origin/main "$lbase"
  runin "$t/lib" "" depends.sh "$t/lib-right.md"
  want "origin/main without origin/HEAD is the base" 0
  git -C "$t/lib" symbolic-ref refs/remotes/origin/HEAD refs/remotes/origin/main
  runin "$t/lib" "" depends.sh "$t/lib-right.md"
  want "origin/HEAD names the base" 0
  git -C "$t/lib" update-ref refs/remotes/origin/trunk "$lbase"
  git -C "$t/lib" update-ref refs/remotes/origin/main HEAD
  git -C "$t/lib" symbolic-ref refs/remotes/origin/HEAD refs/remotes/origin/trunk
  runin "$t/lib" "" depends.sh "$t/lib-right.md"
  want "origin/HEAD is used over origin/main" 0
  git -C "$t/lib" update-ref -d refs/remotes/origin/trunk
  git -C "$t/lib" symbolic-ref --delete refs/remotes/origin/HEAD
  git -C "$t/lib" update-ref -d refs/remotes/origin/main
  git -C "$t/lib" update-ref refs/remotes/origin/master "$lbase"
  runin "$t/lib" "" depends.sh "$t/lib-right.md"
  want "origin/master is the last fallback" 0
  git -C "$t/lib" update-ref -d refs/remotes/origin/master
  runin "$t/lib" "" depends.sh "$t/lib-right.md"
  want "no default branch stops the run" 2
  grep -q 'HOLDOUT_BASE' "$t/out" || { echo "self-test failed: no-base message does not name HOLDOUT_BASE"; fails=$((fails + 1)); }

  # A diff of docs, a lockfile and tests only needs no bindings.
  mkrepo "$t/docs"; mkdir "$t/docs/docs"
  printf 'a\n' > "$t/docs/README.md"; printf '{}\n' > "$t/docs/package-lock.json"
  git -C "$t/docs" add -A; git -C "$t/docs" commit -q -m base
  dbase=$(git -C "$t/docs" rev-parse HEAD)
  printf 'b\n' > "$t/docs/README.md"; printf '{"a":1}\n' > "$t/docs/package-lock.json"
  printf 'c\n' > "$t/docs/docs/g.md"
  git -C "$t/docs" add -A; git -C "$t/docs" commit -q -m change
  runin "$t/docs" "$dbase" depends.sh "$t/none-reason.md"
  want "docs and lockfile only diff passes as none" 0

  # Edits six lines apart are two hunks, not one.
  mkrepo "$t/z"
  seq 1 30 | sed 's/^/l/' > "$t/z/f.py"
  git -C "$t/z" add -A; git -C "$t/z" commit -q -m base
  zbase=$(git -C "$t/z" rev-parse HEAD)
  sed -e 's/^l5$/A on/' -e 's/^l9$/B on/' "$t/z/f.py" > "$t/z/x" && mv "$t/z/x" "$t/z/f.py"
  git -C "$t/z" commit -q -am change
  printf '```holdout\n- test: T1\n  breaks: f.py "A on"\n```\n' > "$t/z1.md"
  printf '```holdout\n- test: T1\n  breaks: f.py "A on"\n- test: T2\n  breaks: f.py "B on"\n```\n' > "$t/z2.md"
  printf '```holdout\n- test: T1\n  breaks: f.py #1\n- test: T2\n  breaks: f.py #2\n```\n' > "$t/z3.md"
  cat > "$t/bin/zboth.sh" <<'X'
#!/bin/sh
if grep -q 'A on' f.py && grep -q 'B on' f.py; then echo "Total: 1"; echo "Failed: 0"; else echo "Total: 1"; echo "Failed: 1"; fi
X
  runin "$t/z" "$zbase" zboth.sh "$t/z1.md"
  want "edits 4 lines apart need two claims" 2
  runin "$t/z" "$zbase" zboth.sh "$t/z2.md"
  want "edits 4 lines apart, two text bindings pass" 0
  runin "$t/z" "$zbase" zboth.sh "$t/z3.md"
  want "edits 4 lines apart, two ordinal bindings pass" 0

  # Files with no text hunk, odd names, and names with spaces.
  mkrepo "$t/f"
  printf 'x\n' > "$t/f/bin.dat"; printf 'a\n' > "$t/f/m.sh"; printf 'q\n' > "$t/f/sp ace.py"
  printf 'q\n' > "$t/f/-dash.py"; printf 'q\n' > "$t/f/:x.py"; printf 'q\n' > "$t/f/$(printf 'ta\tb.py')"
  git -C "$t/f" add -A; git -C "$t/f" commit -q -m base
  fbase=$(git -C "$t/f" rev-parse HEAD)
  printf '\000\001\002' > "$t/f/bin.dat"; chmod +x "$t/f/m.sh"
  printf 'q2\n' > "$t/f/sp ace.py"; printf 'q2\n' > "$t/f/-dash.py"; printf 'q2\n' > "$t/f/:x.py"; printf 'q2\n' > "$t/f/$(printf 'ta\tb.py')"
  git -C "$t/f" commit -q -am change
  all5='- untested: bin.dat\n  why: data\n- untested: m.sh\n  why: mode\n- untested: -dash.py\n  why: x\n- untested: :x.py\n  why: x\n'
  printf "\`\`\`holdout\n$all5- untested: sp ace.py #1\n  why: x\n- untested: ta\tb.py\n  why: x\n\`\`\`\n" > "$t/f-all.md"
  printf '```holdout\nnone: x\n```\n' > "$t/f-none.md"
  runin "$t/f" "$fbase" depends.sh "$t/f-none.md"
  want "none over binary, mode, dash, space and tab names is refused" 2
  for n in bin.dat m.sh -dash.py 'sp ace.py'; do
    grep -qF -- "$n" "$t/out" || { echo "self-test failed: none run does not name $n"; fails=$((fails + 1)); }
  done
  grep -q 'ta.b.py' "$t/out" || { echo "self-test failed: none run does not name the tab file"; fails=$((fails + 1)); }
  runin "$t/f" "$fbase" depends.sh "$t/f-all.md"
  want "every odd file claimed by name passes" 0
  printf "\`\`\`holdout\n- untested: m.sh\n  why: mode\n- untested: -dash.py\n  why: x\n- untested: :x.py\n  why: x\n- untested: sp ace.py #1\n  why: x\n- untested: ta\tb.py\n  why: x\n\`\`\`\n" > "$t/f-nobin.md"
  runin "$t/f" "$fbase" depends.sh "$t/f-nobin.md"
  want "a changed binary file with no claim is unclaimed" 2
  grep -q 'bin.dat has no text hunk' "$t/out" || { echo "self-test failed: binary file not reported as hunkless"; fails=$((fails + 1)); }
  printf "\`\`\`holdout\n- untested: bin.dat\n  why: data\n- untested: -dash.py\n  why: x\n- untested: :x.py\n  why: x\n- untested: sp ace.py #1\n  why: x\n- untested: ta\tb.py\n  why: x\n\`\`\`\n" > "$t/f-nomode.md"
  runin "$t/f" "$fbase" depends.sh "$t/f-nomode.md"
  want "a mode-only change with no claim is unclaimed" 2
  printf "\`\`\`holdout\n- untested: bin.dat\n  why: data\n- untested: m.sh\n  why: mode\n- untested: -dash.py\n  why: x\n- untested: :x.py\n  why: x\n- untested: ta\tb.py\n  why: x\n\`\`\`\n" > "$t/f-nospace.md"
  runin "$t/f" "$fbase" depends.sh "$t/f-nospace.md"
  want "a name with a space is not claimed by other entries" 2
  printf "\`\`\`holdout\n- test: T1\n  breaks: sp ace.py \"q2\"\n- untested: bin.dat\n  why: data\n- untested: m.sh\n  why: mode\n- untested: -dash.py\n  why: x\n- untested: :x.py\n  why: x\n- untested: ta\tb.py\n  why: x\n\`\`\`\n" > "$t/f-bindsp.md"
  runin "$t/f" "$fbase" depends.sh "$t/f-bindsp.md" --dry-run
  want "a binding to a name with a space resolves" 0

  # What counts as production.
  mkrepo "$t/p"; mkdir -p "$t/p/src/latest" "$t/p/src/contests" "$t/p/block" "$t/p/lib" "$t/p/tests" "$t/p/FooTests"
  for f in src/latest/a.py src/contests/b.py block/block.json lib/clock.json tests/t.py FooTests/t.cs package-lock.json README.md; do echo 1 > "$t/p/$f"; done
  git -C "$t/p" add -A; git -C "$t/p" commit -q -m base
  pbase=$(git -C "$t/p" rev-parse HEAD)
  for f in src/latest/a.py src/contests/b.py block/block.json lib/clock.json tests/t.py FooTests/t.cs package-lock.json README.md; do echo 2 >> "$t/p/$f"; done
  git -C "$t/p" commit -q -am change
  runin "$t/p" "$pbase" depends.sh "$t/none-reason.md"
  want "latest/, contests/, block.json and clock.json are production" 2
  for n in src/latest/a.py src/contests/b.py block/block.json lib/clock.json; do
    grep -qF "$n" "$t/out" || { echo "self-test failed: $n not treated as production"; fails=$((fails + 1)); }
  done
  for n in tests/t.py FooTests/t.cs package-lock.json README.md; do
    ! grep -qF "$n" "$t/out" || { echo "self-test failed: $n treated as production"; fails=$((fails + 1)); }
  done
  ( cd "$t/p" && HOLDOUT_NOT_PRODUCTION='src/** block/** lib/**' HOLDOUT_BASE="$pbase" bash "$self" --spec "$t/none-reason.md" >"$t/out" 2>&1 ); rc=$?
  want "HOLDOUT_NOT_PRODUCTION replaces the list, so tests and markdown are production" 2
  grep -q 'README.md' "$t/out" || { echo "self-test failed: replaced list still exempts README.md"; fails=$((fails + 1)); }
  ( cd "$t/p" && HOLDOUT_NOT_PRODUCTION='src/** block/** lib/** tests/** FooTests/** package-lock.json README.md' HOLDOUT_BASE="$pbase" bash "$self" --spec "$t/none-reason.md" >"$t/out" 2>&1 ); rc=$?
  want "HOLDOUT_NOT_PRODUCTION can exempt everything" 0

  # HOLDOUT_PRODUCTION: lists what it leaves out, and refuses colons.
  mkrepo "$t/n"; mkdir "$t/n/lib" "$t/n/src"
  echo 1 > "$t/n/lib/x.py"; echo 1 > "$t/n/src/y.py"
  git -C "$t/n" add -A; git -C "$t/n" commit -q -m base
  nbase=$(git -C "$t/n" rev-parse HEAD)
  echo 2 > "$t/n/lib/x.py"; echo 2 > "$t/n/src/y.py"
  git -C "$t/n" commit -q -am change
  ( cd "$t/n" && HOLDOUT_PRODUCTION=lib HOLDOUT_BASE="$nbase" bash "$self" --spec "$t/lib-right.md" >"$t/out" 2>&1 ); rc=$?
  want "narrowing to lib passes" 0
  grep -q 'left out.*src/y.py' "$t/out" || { echo "self-test failed: narrowed-out file not listed"; sed 's/^/  /' "$t/out"; fails=$((fails + 1)); }
  ( cd "$t/n" && HOLDOUT_PRODUCTION=lib:src HOLDOUT_BASE="$nbase" bash "$self" --spec "$t/lib-right.md" >"$t/out" 2>&1 ); rc=$?
  want "colon in HOLDOUT_PRODUCTION is refused" 2
  grep -q 'space-separated' "$t/out" || { echo "self-test failed: colon message missing"; fails=$((fails + 1)); }

  # With the revert step removed the held-out tree equals the clean one, so even the test that
  # depends on its hunk passes there and the run must report it as caught.
  sed 's/git apply -R --unidiff-zero "\$patch_file"/true/' "$self" > "$t/no-revert.sh"
  if cmp -s "$self" "$t/no-revert.sh"; then
    echo "self-test failed: the revert mutation did not apply"; fails=$((fails + 1))
  else
    run depends.sh "$t/no-revert.sh" "$t/spec.md"
    want "run without the revert step fails" 1
  fi

  [ "$fails" = 0 ] && echo "self-test passed" && return 0
  return 1
}

if [ "${1:-}" = "--self-test" ]; then self_test; exit $?; fi

# Resolve the repository from the working directory, not from where this file happens to live. A
# copy of this script run from outside its checkout used to cd to the wrong place and report
# "cannot find merge base", which reads as a git problem rather than as a path problem.
repo_root=$(git rev-parse --show-toplevel 2>/dev/null) || {
  echo "holdout: not inside a git repository"; exit 2; }
cd "$repo_root"

spec=""
dry_run=0
while [ $# -gt 0 ]; do
  case "$1" in
    --spec) shift; [ $# -gt 0 ] || { echo "holdout: --spec needs a file"; exit 2; }; spec="$1"; shift ;;
    --dry-run) dry_run=1; shift ;;
    *) echo "holdout: unrecognised argument '$1'"; exit 2 ;;
  esac
done

[ -n "$spec" ] || { echo "holdout: --spec is required"; exit 2; }
[ -f "$spec" ] || { echo "holdout: spec file not found: $spec"; exit 2; }

# HOLDOUT_BASE wins. Otherwise the remote's default branch: origin/HEAD, then origin/main, then
# origin/master. Full ref names, so a local branch called origin/main cannot shadow the remote one.
base_ref="${HOLDOUT_BASE:-}"
if [ -z "$base_ref" ]; then
  for cand in "$(git symbolic-ref -q refs/remotes/origin/HEAD 2>/dev/null)" \
      refs/remotes/origin/main refs/remotes/origin/master; do
    if [ -n "$cand" ] && git rev-parse --verify -q "$cand^{commit}" >/dev/null 2>&1; then
      base_ref="$cand"; break
    fi
  done
  [ -n "$base_ref" ] || {
    echo "holdout: no base branch found: origin/HEAD is unset and there is no origin/main or origin/master."
    echo "         Set HOLDOUT_BASE to the branch this change started from."
    exit 2; }
fi
base=$(git merge-base "$base_ref" HEAD 2>/dev/null) || {
  echo "holdout: cannot find merge base with $base_ref"; exit 2; }

# Paths that are not production code: tests, docs, lockfiles. The one place to edit them.
# Space-separated globs; ** crosses directories. HOLDOUT_NOT_PRODUCTION replaces the whole list.
# Production is every other changed file; HOLDOUT_PRODUCTION (space-separated pathspecs) narrows it.
DEFAULT_NOT_PRODUCTION='**/tests/** **/Tests/** **/test/** **/Test/** **/*Tests/** **/*Test/**
  **/__tests__/** **/*.test.js **/*.test.jsx **/*.test.ts **/*.test.tsx **/*.test.mjs
  **/*.spec.js **/*.spec.jsx **/*.spec.ts **/*.spec.tsx **/*.spec.mjs **/*_spec.rb
  **/*_test.* **/test_*.py **/*.md **/*.mdx **/*.rst docs/** LICENSE*
  **/package-lock.json **/packages.lock.json **/npm-shrinkwrap.json **/yarn.lock **/pnpm-lock.yaml
  **/bun.lockb **/Cargo.lock **/go.sum **/poetry.lock **/Pipfile.lock **/Gemfile.lock
  **/composer.lock'

for v in HOLDOUT_PRODUCTION HOLDOUT_NOT_PRODUCTION; do
  case "${!v:-}" in
    *:*) echo "holdout: $v contains a colon. Patterns are space-separated, not colon-separated."; exit 2 ;;
  esac
done

NOT_PRODUCTION=()
set -f
for pat in ${HOLDOUT_NOT_PRODUCTION:-$DEFAULT_NOT_PRODUCTION}; do NOT_PRODUCTION+=(":(exclude,glob)$pat"); done
PROD_SPEC=()
for pat in ${HOLDOUT_PRODUCTION:-.}; do PROD_SPEC+=("$pat"); done
set +f

SEP=$(printf '\037')   # field separator that cannot appear in a name the way a tab can

# Names come NUL-separated so odd characters in a name cannot hide a file.
diff_names() { git -c core.quotepath=off diff -z --name-only --no-renames "$base" -- "$@" 2>/dev/null; }
file_diff() {  # one file's diff with no context, so each separate edit is its own hunk
  git --literal-pathspecs -c core.quotepath=off diff --unified=0 --no-renames "$base" -- "$1" 2>/dev/null
}
all_files=(); cand_files=(); prod_files=()
while IFS= read -r -d '' f; do all_files+=("$f"); done < <(diff_names .)
while IFS= read -r -d '' f; do cand_files+=("$f"); done < <(diff_names . "${NOT_PRODUCTION[@]}")
while IFS= read -r -d '' f; do prod_files+=("$f"); done < <(diff_names "${PROD_SPEC[@]}" "${NOT_PRODUCTION[@]}")

# Changed code that HOLDOUT_PRODUCTION leaves out. Not a failure, but never silent.
report_left_out() {
  local f g found
  for f in ${cand_files[@]+"${cand_files[@]}"}; do
    found=0
    for g in ${prod_files[@]+"${prod_files[@]}"}; do [ "$f" = "$g" ] && found=1; done
    [ "$found" -eq 1 ] || echo "holdout: left out by HOLDOUT_PRODUCTION, not checked: $f"
  done
}
# A production setting that matches nothing while code changed would let the run pass over it.
if [ ${#prod_files[@]} -eq 0 ] && [ ${#cand_files[@]} -gt 0 ]; then
  echo "holdout: no production hunks matched, but these changed files are not tests or docs:"
  for f in "${cand_files[@]}"; do echo "         $f"; done
  echo "         HOLDOUT_PRODUCTION='${HOLDOUT_PRODUCTION:-}' covers none of them."
  exit 2
fi

# split_target <text>: sets sp_path and sp_anchor. The path is the longest changed file the text
# starts with, so a name with a space works; otherwise the text up to the first space.
split_target() {
  local text="$1" f best=""
  for f in ${all_files[@]+"${all_files[@]}"}; do
    if [ "$text" = "$f" ] || [ "${text#"$f" }" != "$text" ]; then
      [ ${#f} -gt ${#best} ] && best="$f"
    fi
  done
  if [ -n "$best" ]; then
    sp_path="$best"; sp_anchor="${text#"$best"}"; sp_anchor="${sp_anchor# }"
  else
    sp_path="${text%% *}"; sp_anchor="${text#"$sp_path"}"; sp_anchor="${sp_anchor# }"
  fi
}

# ---------------------------------------------------------------------------
# Parse the ```holdout block. Two directives: "test:/breaks:" pairs and
# "untested:/why:" pairs. Anything else in the block is an error, not a skip.
# ---------------------------------------------------------------------------
block=$(awk '/^```holdout[[:space:]]*$/{f=1;next} /^```[[:space:]]*$/{if(f)exit} f' "$spec")

if [ -z "$block" ]; then
  echo "holdout: no \`\`\`holdout block in $spec"
  echo "         A PR touching production code must bind each new test to the hunk it depends on,"
  echo "         or list the hunk under untested: with a reason."
  exit 2
fi

bindings=()   # each entry: "test<TAB>path<TAB>anchor"
untested=()   # each entry: "path<TAB>anchor"
cur_test=""
cur_untested=""
none=0
none_reason=""

while IFS= read -r line; do
  case "$line" in
    "- test: "*)      cur_test="${line#- test: }"; cur_untested="" ;;
    "- untested: "*)  cur_untested="${line#- untested: }"; cur_test="" ;;
    *"breaks: "*)
      [ -n "$cur_test" ] || { echo "holdout: 'breaks:' with no preceding 'test:'"; exit 2; }
      split_target "${line#*breaks: }"
      bindings+=("$cur_test${SEP}$sp_path${SEP}$sp_anchor")
      cur_test=""
      ;;
    *"why: "*)
      [ -n "$cur_untested" ] || { echo "holdout: 'why:' with no preceding 'untested:'"; exit 2; }
      split_target "$cur_untested"
      untested+=("$sp_path${SEP}$sp_anchor")
      cur_untested=""
      ;;
    "none:"*)
      # A change with no production hunks at all still declares that, with a reason. Deliberately
      # explicit: a missing block and a change that needs no bindings must not look the same, or
      # every missing block reads as the second.
      none_reason="${line#none:}"
      none_reason="${none_reason# }"
      [ -n "$none_reason" ] || { echo "holdout: 'none:' needs a reason"; exit 2; }
      none=1
      ;;
    ""|"#"*) ;;
    *) echo "holdout: unparsed line in block: $line"; exit 2 ;;
  esac
done <<< "$block"

if [ "$none" -eq 1 ] && { [ ${#bindings[@]} -gt 0 ] || [ ${#untested[@]} -gt 0 ]; }; then
  echo "holdout: 'none:' cannot be combined with bindings."
  exit 2
fi

if [ "$none" -eq 1 ]; then
  # Still check the diff: a change claiming no production hunks must actually have none, or the
  # declaration is just a way of opting out.
  if [ ${#prod_files[@]} -gt 0 ]; then
    echo "holdout: the block says none, but this change touches production files:"
    for f in "${prod_files[@]}"; do echo "         $f"; done
    exit 2
  fi
  echo "holdout: none declared ($none_reason), and the diff touches no production file"
  exit 0
fi

if [ ${#bindings[@]} -eq 0 ] && [ ${#untested[@]} -eq 0 ]; then
  echo "holdout: the block is empty, so this would pass having held out nothing."
  exit 2
fi

echo "holdout: $((${#bindings[@]})) binding(s), $((${#untested[@]})) untested hunk(s), base ${base:0:8}"

# hunk_ordinal <path> <anchor>: prints the 1-based hunk ordinal the anchor names in that file's
# diff against the base. Works for added files too (one hunk). Non-zero means unresolved.
hunk_ordinal() {
  local path="$1" anchor="$2" diff_file count
  diff_file=$(mktemp)
  file_diff "$path" > "$diff_file"
  if [ ! -s "$diff_file" ]; then
    rm -f "$diff_file"; echo "holdout: no diff for $path against the base" >&2; return 1
  fi
  count=$(grep -c '^@@' "$diff_file" || true)
  if [[ "$anchor" == \#* ]]; then
    local want="${anchor#\#}"
    if ! [[ "$want" =~ ^[0-9]+$ ]] || [ "$want" -lt 1 ] || [ "$want" -gt "$count" ]; then
      rm -f "$diff_file"; echo "holdout: $path has $count hunk(s), no $anchor" >&2; return 1
    fi
    rm -f "$diff_file"; echo "$want"; return 0
  fi
  local needle="${anchor%\"}"; needle="${needle#\"}"
  [ -n "$needle" ] || { rm -f "$diff_file"; echo "holdout: empty anchor for $path" >&2; return 1; }
  local i=0 hits=0 first=0 l
  while IFS= read -r l; do
    case "$l" in
      @@*) i=$((i+1)) ;;
      +*|-*) if [ "$i" -gt 0 ] && [[ "$l" != "+++"* ]] && [[ "$l" != "---"* ]] && [[ "$l" == *"$needle"* ]]; then
            if [ "$first" -ne "$i" ]; then hits=$((hits+1)); first="$i"; fi
          fi ;;
    esac
  done < "$diff_file"
  rm -f "$diff_file"
  if [ "$hits" -eq 0 ]; then echo "holdout: anchor $anchor matches no changed line in $path" >&2; return 1; fi
  if [ "$hits" -gt 1 ]; then echo "holdout: anchor $anchor is ambiguous, matches $hits hunks in $path" >&2; return 1; fi
  echo "$first"
}

# ---------------------------------------------------------------------------
# Resolve an anchor to exactly one hunk of one file's diff against the base.
# Anchor forms: "some added line substring"   (quoted content anchor)
#               #2                            (1-based hunk ordinal)
# Line numbers are deliberately not supported: they do not survive a rebase.
# Prints the hunk's patch on stdout. Non-zero means unresolved.
# ---------------------------------------------------------------------------
resolve_hunk() {
  local path="$1" anchor="$2"
  local diff_file; diff_file=$(mktemp)
  file_diff "$path" > "$diff_file"

  if [ ! -s "$diff_file" ]; then
    rm -f "$diff_file"; echo "holdout: no diff for $path against the base" >&2; return 1
  fi

  # A file the change adds outright is one hunk covering the whole file, and holding it out deletes
  # the file. In a compiled language that never builds, so the run can only end inconclusive. Say so
  # here rather than after spending two builds discovering it. Measured on a real feature branch:
  # every one of its ten production files was new, so every binding cost 29 seconds to learn nothing.
  if grep -q '^--- /dev/null' "$diff_file"; then
    rm -f "$diff_file"
    echo "holdout: $path is added by this change, so holding any of it out deletes the file." >&2
    echo "         Nothing can be proven that way. Bind a test to a hunk that modifies an existing" >&2
    echo "         file, or list this one under untested: with a reason." >&2
    return 1
  fi

  local header; header=$(sed -n '1,/^@@/p' "$diff_file" | sed '$d')
  local count; count=$(grep -c '^@@' "$diff_file")
  local want=0

  if [[ "$anchor" == \#* ]]; then
    want="${anchor#\#}"
    if ! [[ "$want" =~ ^[0-9]+$ ]] || [ "$want" -lt 1 ] || [ "$want" -gt "$count" ]; then
      rm -f "$diff_file"; echo "holdout: $path has $count hunk(s), no $anchor" >&2; return 1
    fi
  else
    local needle="${anchor%\"}"; needle="${needle#\"}"
    [ -n "$needle" ] || { rm -f "$diff_file"; echo "holdout: empty anchor for $path" >&2; return 1; }
    local i=0 hits=0 first=0
    while IFS= read -r l; do
      case "$l" in
        @@*) i=$((i+1)) ;;
        +*) if [ "$i" -gt 0 ] && [[ "$l" == *"$needle"* ]]; then
              if [ "$first" -ne "$i" ]; then hits=$((hits+1)); first="$i"; fi
            fi ;;
      esac
    done < "$diff_file"
    if [ "$hits" -eq 0 ]; then
      rm -f "$diff_file"; echo "holdout: anchor $anchor matches no added line in $path" >&2; return 1
    fi
    if [ "$hits" -gt 1 ]; then
      rm -f "$diff_file"; echo "holdout: anchor $anchor is ambiguous, matches $hits hunks in $path" >&2; return 1
    fi
    want="$first"
  fi

  printf '%s\n' "$header"
  awk -v want="$want" '/^@@/{n++} n==want' "$diff_file"
  rm -f "$diff_file"
  return 0
}

# ---------------------------------------------------------------------------
# Every production hunk must be claimed: bound to a test, or listed as untested.
# An unclaimed hunk is a silent gap; naming it makes the omission a written claim.
# ---------------------------------------------------------------------------
# Claims are per hunk. A binding or untested entry claims the one hunk its anchor names. An
# untested entry with no anchor exempts its whole file, which is also how a file with no text hunk
# (binary, mode change) is claimed.
claims=""   # lines "path<TAB>ordinal"
whole=""    # one path per line
bad=0
for b in ${bindings[@]+"${bindings[@]}"}; do
  p=$(printf '%s' "$b" | cut -d "$SEP" -f2); a=$(printf '%s' "$b" | cut -d "$SEP" -f3)
  if o=$(hunk_ordinal "$p" "$a"); then claims="$claims$p${SEP}$o
"; else bad=$((bad+1)); fi
done
for u in ${untested[@]+"${untested[@]}"}; do
  p=$(printf '%s' "$u" | cut -d "$SEP" -f1); a=$(printf '%s' "$u" | cut -d "$SEP" -f2)
  if [ -z "$a" ]; then
    if [ -n "$(file_diff "$p")" ]; then whole="$whole$p
"; else echo "holdout: no diff for $p against the base" >&2; bad=$((bad+1)); fi
  elif o=$(hunk_ordinal "$p" "$a"); then claims="$claims$p${SEP}$o
"; else bad=$((bad+1)); fi
done
[ "$bad" -eq 0 ] || { echo "holdout: $bad anchor(s) unresolved, so nothing was proven for them"; exit 2; }

unclaimed=0
for f in ${prod_files[@]+"${prod_files[@]}"}; do
  printf '%s' "$whole" | grep -qxF -- "$f" && continue
  hdrs=$(file_diff "$f" | awk '
    /^@@/ { n++; split($3, a, ","); ln = substr(a[1], 2) + 0; print ln }')
  if [ -z "$hdrs" ]; then
    echo "holdout: UNCLAIMED  $f has no text hunk (binary, mode change) and is not listed untested:"
    unclaimed=$((unclaimed+1)); continue
  fi
  k=0
  while IFS= read -r line; do
    k=$((k+1))
    if ! printf '%s' "$claims" | grep -qxF -- "$f${SEP}$k"; then
      echo "holdout: UNCLAIMED  $f line $line (hunk $k) is bound to no test and not listed untested:"
      unclaimed=$((unclaimed+1))
    fi
  done <<< "$hdrs"
done
[ "$unclaimed" -eq 0 ] || { echo "holdout: $unclaimed production hunk(s) unclaimed"; exit 2; }

if [ "$dry_run" -eq 1 ]; then
  echo "holdout: --dry-run, resolving anchors without running tests"
  rc=0
  for b in ${bindings[@]+"${bindings[@]}"}; do
    t=$(printf '%s' "$b" | cut -d "$SEP" -f1); p=$(printf '%s' "$b" | cut -d "$SEP" -f2); a=$(printf '%s' "$b" | cut -d "$SEP" -f3)
    if patch=$(resolve_hunk "$p" "$a"); then
      lines=$(printf '%s\n' "$patch" | grep -c '^+' || true)
      echo "holdout: ok        $t -> $p $a ($lines added line(s))"
    else
      rc=2
    fi
  done
  report_left_out
  exit $rc
fi

# ---------------------------------------------------------------------------
# Run each binding in a throwaway worktree at HEAD.
# ---------------------------------------------------------------------------
wt=$(mktemp -d)
cleanup() { git worktree remove --force "$wt" >/dev/null 2>&1 || rm -rf "$wt"; }
trap cleanup EXIT

git worktree add --detach "$wt" HEAD >/dev/null 2>&1 || { echo "holdout: cannot create worktree"; exit 2; }

run_class() {  # prints total:failed, or "none" when no summary line appeared
  local dir="$1" class="$2" out; out=$(mktemp)
  # ADAPT: run exactly the named test(s) and nothing else. Must print a summary this can parse.
  ( cd "$dir" && $HOLDOUT_TEST_CMD "$class" ) > "$out" 2>&1
  local total failed
  total=$(grep -oE 'Total: [0-9]+' "$out" | tail -1 | grep -oE '[0-9]+' || true)
  failed=$(grep -oE 'Failed: [0-9]+' "$out" | tail -1 | grep -oE '[0-9]+' || echo 0)
  rm -f "$out"
  [ -n "$total" ] || { echo "none"; return; }
  echo "${total}:${failed:-0}"
}

# Every hunk declared untested and nothing bound. That is a legitimate answer for a change that
# ships data rather than behaviour, so it passes, but it must not borrow the words of a run that
# held something out. Say what happened, and stop before spending a build proving nothing.
if [ ${#bindings[@]} -eq 0 ]; then
  echo "holdout: no bindings. ${#untested[@]} hunk(s) declared untested, nothing was held out."
  for u in ${untested[@]+"${untested[@]}"}; do
    echo "         untested  $(printf '%s' "$u" | cut -d "$SEP" -f1) $(printf '%s' "$u" | cut -d "$SEP" -f2)"
  done
  report_left_out
  exit 0
fi

echo "== holdout: building the clean tree =="
( cd "$wt" && eval "$HOLDOUT_BUILD_CMD" >/dev/null 2>&1 ) \
  || { echo "holdout: the clean tree does not build, nothing can be proven"; exit 3; }

status=0
for b in ${bindings[@]+"${bindings[@]}"}; do
  test_id=$(printf '%s' "$b" | cut -d "$SEP" -f1)
  path=$(printf '%s' "$b" | cut -d "$SEP" -f2)
  anchor=$(printf '%s' "$b" | cut -d "$SEP" -f3)
  echo "== holdout: $test_id -> $path $anchor =="

  patch_file=$(mktemp)
  if ! resolve_hunk "$path" "$anchor" > "$patch_file"; then
    rm -f "$patch_file"; status=2; continue
  fi

  # 1. clean run: the test must exist and pass, or the rest proves nothing.
  clean=$(run_class "$wt" "$test_id")
  if [ "$clean" = "none" ]; then
    echo "holdout: UNRESOLVED  $test_id produced no summary line"; rm -f "$patch_file"; status=2; continue
  fi
  c_total="${clean%%:*}"; c_failed="${clean##*:}"
  if [ "$c_total" -eq 0 ]; then
    echo "holdout: UNRESOLVED  $test_id matched 0 tests"; rm -f "$patch_file"; status=2; continue
  fi
  if [ "$c_failed" -ne 0 ]; then
    echo "holdout: INCONCLUSIVE  $test_id already fails on the clean tree ($c_failed of $c_total)"
    # Every test in a class failing is rarely the change's doing. An integration class needs
    # Testcontainers, and a stopped Docker fails all of them identically, which reads as a broken
    # branch unless the message says otherwise.
    if [ "$c_failed" -eq "$c_total" ] && ! docker info >/dev/null 2>&1; then
      echo "                       All of them failed and the Docker daemon is unreachable."
      echo "                       An integration class needs Testcontainers. Start Docker, or bind"
      echo "                       a unit test class instead."
    fi
    rm -f "$patch_file"; status=3; continue
  fi

  # 2. hold the hunk out.
  if ! ( cd "$wt" && git apply -R --unidiff-zero "$patch_file" 2>/dev/null ); then
    echo "holdout: UNRESOLVED  the hunk does not apply in reverse (rebase drift?)"
    rm -f "$patch_file"; status=2; continue
  fi

  # 3. rebuild. A held-out hunk that breaks the build proves nothing either way.
  if ! ( cd "$wt" && eval "$HOLDOUT_BUILD_CMD" >/dev/null 2>&1 ); then
    echo "holdout: INCONCLUSIVE  the tree does not build with $path $anchor held out."
    echo "                       Bind a behaviour line, not a signature or a declaration."
    ( cd "$wt" && git checkout -- . >/dev/null 2>&1 )
    rm -f "$patch_file"; status=3; continue
  fi

  # 4. the named test must now fail.
  held=$(run_class "$wt" "$test_id")
  h_total="${held%%:*}"; h_failed="${held##*:}"

  if [ "$held" = "none" ] || [ "$h_total" -eq 0 ]; then
    echo "holdout: UNRESOLVED  $test_id ran 0 tests with the hunk held out"
    status=2
  elif [ "$h_failed" -eq 0 ]; then
    echo "holdout: CAUGHT      $test_id passed with its hunk held out"
    echo "                     hunk     $path $anchor"
    echo "                     clean    $c_total ran, 0 failed"
    echo "                     held out $h_total ran, 0 failed   <- must fail here"
    echo "                     The test does not depend on the code it claims to test."
    status=1
  else
    echo "holdout: ok          $test_id failed as required ($h_failed of $h_total)"
  fi

  # 5. restore and confirm. A restore that does not restore is its own defect.
  ( cd "$wt" && git checkout -- . >/dev/null 2>&1 )
  ( cd "$wt" && eval "$HOLDOUT_BUILD_CMD" >/dev/null 2>&1 ) \
    || { echo "holdout: INCONCLUSIVE  the tree does not build after restore"; status=3; }
  rm -f "$patch_file"
done

case "$status" in
  0) echo "holdout: ${#bindings[@]} binding(s) held out, each named test failed and passed again on restore" ;;
  1) echo "holdout: a test passed with its hunk held out" ;;
  2) echo "holdout: a binding could not be resolved, so nothing was proven for it" ;;
  3) echo "holdout: inconclusive, the held-out tree did not build" ;;
esac
report_left_out
exit $status
