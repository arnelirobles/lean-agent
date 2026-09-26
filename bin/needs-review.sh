#!/usr/bin/env bash
# Prints one line per review rule the current change fires, so the diff decides whether a critic
# runs, not the ticket title. Advisory: always exits 0.
#
#   needs-review.sh [base]         (base defaults to $LEAN_BASE, then origin/HEAD, origin/main, origin/master)
#   needs-review.sh --self-test
#
# Rules live in the repository, in .lean/needs-review.rules (start from examples/needs-review.rules
# in this plugin). Each rule is an INI section:
#
#   [secrets]
#   label   = secret handling touched
#   path    = regex over the repo-relative path (alone: fires when a changed file matches;
#             with added or removed: limits which files those apply to)
#   added   = regex over added lines
#   removed = regex over removed lines (for a test that loses an assertion)
#
# Rules sharing a label print as one line, so "this path, or this kind of line" is two sections
# with the same label. The rules file itself is never scanned: its own regexes would fire.
#
# What it compares: the working tree against the merge base with the base branch, plus untracked
# files folded in as new files. A rule that reads only committed changes misses the file an agent
# has written and not yet added. That also means running it in a checkout full of unrelated work
# walks all of it, so run it in a clean copy of the branch.
#
# Output contract. Stdout holds only lines a reviewer should act on, so "any output means a critic
# runs" stays true. That includes the fail-safe cases: no rules file, no rules in it, or no merge
# base each print a line on stdout, because a check that could not run must not look like a change
# that fired nothing. The count of files examined goes to stderr.
#
# The diff reaches Python through a file and the program through -c. `python3 - <<EOF` reads the
# program from stdin, so a pipe into it is swallowed and the script reports nothing on every diff,
# which one earlier version of this script did.

set -uo pipefail

rules_py=$(cat <<'PY'
import configparser, re, sys

rules_path, diff_path, paths_path = sys.argv[1], sys.argv[2], sys.argv[3]
cp = configparser.ConfigParser(interpolation=None, delimiters=("=",), comment_prefixes=("#", ";"), inline_comment_prefixes=None)
cp.optionxform = str
try:
    cp.read_string(open(rules_path, encoding="utf-8", errors="replace").read(), source=rules_path)
except configparser.Error as ex:
    print("needs-review: cannot parse %s (%s); nothing was checked" % (rules_path, str(ex).splitlines()[0]))
    sys.exit(0)

rules = []
for name in cp.sections():
    s = cp[name]
    try:
        r = {k: re.compile(s[k].strip()) for k in ("path", "added", "removed") if s.get(k, "").strip()}
    except re.error as ex:
        print("needs-review: rule [%s] has a bad regex (%s); nothing was checked" % (name, ex))
        sys.exit(0)
    if not r:
        continue
    rules.append((name, s.get("label", name).strip(), r))
if not rules:
    print("needs-review: %s holds no rules; nothing was checked" % rules_path)
    sys.exit(0)

# The path list comes from git's name list, not the patch: a binary file or a pure rename has no
# ---/+++ lines in a patch, and a path rule must still see it.
paths = sorted({p for p in open(paths_path, encoding="utf-8", errors="replace").read().split("\0") if p})
added = {p: [] for p in paths}
removed = {p: [] for p in paths}
cur = old = None
for line in open(diff_path, encoding="utf-8", errors="replace").read().splitlines():
    if line.startswith("--- "):
        p = line[4:]
        old = None if p == "/dev/null" else (p[2:] if p.startswith("a/") else p)
        continue
    if line.startswith("+++ "):
        p = line[4:]
        cur = old if p == "/dev/null" else (p[2:] if p.startswith("b/") else p)
        if cur and cur not in added:
            cur = None
        continue
    if cur is None or line.startswith("@@"):
        continue
    if line.startswith("+"):
        added[cur].append(line[1:])
    elif line.startswith("-"):
        removed[cur].append(line[1:])

fired = {}
for name, label, r in rules:
    hits = fired.setdefault(label, set())
    for p in paths:
        if "path" in r and not r["path"].search(p):
            continue
        if "added" not in r and "removed" not in r:
            hits.add(p)
        elif ("added" in r and any(r["added"].search(l) for l in added[p])) or \
             ("removed" in r and any(r["removed"].search(l) for l in removed[p])):
            hits.add(p)
for label, hits in fired.items():
    if hits:
        print("needs-review: %s: %s" % (label, ", ".join(sorted(hits))))
print("needs-review: %d changed file(s) examined against %d rule(s)" % (len(paths), len(rules)), file=sys.stderr)
PY
)

default_base() {
  local b
  if [ -n "${LEAN_BASE:-}" ]; then echo "$LEAN_BASE"; return; fi
  b=$(git symbolic-ref -q --short refs/remotes/origin/HEAD 2>/dev/null) && { echo "$b"; return; }
  for b in origin/main origin/master main master; do
    git rev-parse -q --verify "$b^{commit}" >/dev/null 2>&1 && { echo "$b"; return; }
  done
}

run() { # $1 optional base; runs in the current repository
  local top base mb rules diff_file paths_file rc
  top=$(git rev-parse --show-toplevel 2>/dev/null) || { echo "needs-review: not inside a git repository; nothing was checked"; return 0; }
  cd "$top" || return 0
  rules=".lean/needs-review.rules"
  if [ ! -f "$rules" ]; then
    echo "needs-review: no $rules in this repository; nothing was checked. Copy examples/needs-review.rules from the lean-agent plugin and edit it."
    return 0
  fi
  base=${1:-$(default_base)}
  mb=$( [ -n "$base" ] && git merge-base "$base" HEAD 2>/dev/null ) || mb=""
  if [ -z "$mb" ]; then
    echo "needs-review: no merge base with '${base:-a default branch}'; nothing was checked. Pass the base branch as an argument."
    return 0
  fi
  diff_file=$(mktemp); paths_file=$(mktemp)
  {
    git diff --name-only --no-renames -z "$mb" -- . ":(exclude)$rules"
    git ls-files --others --exclude-standard -z -- . ":(exclude)$rules"
  } > "$paths_file"
  {
    git diff --no-color --no-ext-diff --no-renames "$mb" -- . ":(exclude)$rules"
    git ls-files --others --exclude-standard -z | while IFS= read -r -d '' f; do
      [ -f "$f" ] && [ "$f" != "$rules" ] || continue
      git diff --no-color --no-index -- /dev/null "$f" 2>/dev/null
    done
  } > "$diff_file"
  python3 -c "$rules_py" "$rules" "$diff_file" "$paths_file" 2>"$diff_file.err"
  rc=$?
  if [ "$rc" != 0 ]; then
    echo "needs-review: the rule check crashed ($(tail -1 "$diff_file.err")); nothing was checked"
  else
    cat "$diff_file.err" >&2
  fi
  rm -f "$diff_file" "$diff_file.err" "$paths_file"
  return 0
}

self_test() {
  local out err fails=0
  root=$(mktemp -d)
  trap 'rm -rf "$root"' EXIT
  o=$root/out; t=$root/repo; mkdir -p "$t" "$o"
  (
    cd "$t" && git init -q -b main . && git config user.email t@example.com && git config user.name t
    mkdir -p src/auth tests .lean
    printf 'def login(user):\n    return True\n' > src/auth/login.py
    printf 'def test_a():\n    assert 1 == 1\n    assert 2 == 2\n' > tests/test_a.py
    printf 'print("hello")\n' > src/app.py
    printf 'def old():\n    pass\n' > src/auth/legacy.py
    printf '\000\001\002' > src/auth/keystore.bin
    git add -A && git commit -qm base && git branch base
  ) || { echo "self-test failed: fixture setup"; return 1; }
  (cd "$t" && run base) > "$o/out0" 2>&1
  grep -q 'no .lean/needs-review.rules' "$o/out0" || { echo "self-test failed: missing rules file not reported: $(cat "$o/out0")"; fails=$((fails + 1)); }

  cp "$(dirname "$(readlink -f "$0")")/../examples/needs-review.rules" "$t/.lean/needs-review.rules" 2>/dev/null || {
    echo "self-test failed: examples/needs-review.rules not found next to the script"; return 1; }
  (cd "$t" && run base) > "$o/out1" 2> "$o/err1"
  [ ! -s "$o/out1" ] || { echo "self-test failed: unchanged tree fired: $(cat "$o/out1")"; fails=$((fails + 1)); }
  grep -q '0 changed file' "$o/err1" || { echo "self-test failed: no examined count: $(cat "$o/err1")"; fails=$((fails + 1)); }

  (
    cd "$t" && printf 'def login(user):\n    return user.password == "x"\n' > src/auth/login.py
    printf 'def test_a():\n    assert 1 == 1\n' > tests/test_a.py
    printf 'import threading\nt = threading.Thread(target=print)\n' > src/worker.py
    printf 'print("hello")\n# a plain change\n' > src/app.py
    git mv src/auth/legacy.py src/legacy.py
  )
  out=$( (cd "$t/src" && run base) 2>"$o/err2")
  for want in 'authentication or permissions.*src/auth/login.py' 'secrets.*src/auth/login.py' \
              'concurrency.*src/worker.py' 'assertion removed.*tests/test_a.py'; do
    grep -qE "$want" <<<"$out" || { echo "self-test failed: want /$want/ in: $out"; fails=$((fails + 1)); }
  done
  grep -q 'src/app.py' <<<"$out" && { echo "self-test failed: a plain change fired a rule: $out"; fails=$((fails + 1)); }
  grep -q 'authentication or permissions.*src/auth/legacy.py' <<<"$out" || { echo "self-test failed: a pure rename out of a watched path did not fire: $out"; fails=$((fails + 1)); }
  grep -q '6 changed file' "$o/err2" || { echo "self-test failed: want 6 files examined: $(cat "$o/err2")"; fails=$((fails + 1)); }

  (cd "$t" && git add -A && git commit -qm change && git branch -f base && printf '\003\004' > src/auth/keystore.bin)
  out=$( (cd "$t" && run base) 2>/dev/null)
  grep -q 'authentication or permissions.*src/auth/keystore.bin' <<<"$out" || { echo "self-test failed: a binary change under a watched path did not fire: $out"; fails=$((fails + 1)); }

  printf '# r\xe9gles en Latin-1\n' | cat - "$t/.lean/needs-review.rules" > "$o/latin1" && cp "$o/latin1" "$t/.lean/needs-review.rules"
  out=$( (cd "$t" && run base) 2>/dev/null)
  grep -q 'keystore.bin' <<<"$out" || { echo "self-test failed: a Latin-1 rules file did not work: $out"; fails=$((fails + 1)); }
  chmod 000 "$t/.lean/needs-review.rules"
  if ! cat "$t/.lean/needs-review.rules" >/dev/null 2>&1; then
    out=$( (cd "$t" && run base) 2>/dev/null)
    grep -q 'the rule check crashed.*nothing was checked' <<<"$out" || { echo "self-test failed: a crash was silent: '$out'"; fails=$((fails + 1)); }
  fi
  chmod 644 "$t/.lean/needs-review.rules"

  printf '# nothing\n' > "$t/.lean/needs-review.rules"
  (cd "$t" && run base) | grep -q 'holds no rules' || { echo "self-test failed: empty rules file not reported"; fails=$((fails + 1)); }
  (cd "$t" && run no-such-branch) | grep -q 'no merge base' || { echo "self-test failed: bad base not reported"; fails=$((fails + 1)); }
  [ "$fails" = 0 ] && echo "self-test passed" && return 0
  return 1
}

if [ "${1:-}" = "--self-test" ]; then self_test; exit $?; fi
run "${1:-}"
exit 0
